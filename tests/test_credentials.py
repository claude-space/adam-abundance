"""Credentials plane (PRD §8, §9.1, §11): env/file resolution, secret redaction
registration, GCP Secret Manager fallback, and the typed-accessor defaults.

Needs the stack installed (python-dotenv) but NO database. The Secret Manager
backend is exercised with the GCP client mocked (either an instance-level stub or
an injected fake ``google.cloud.secretmanager`` module) — GCP is never contacted.

``Credentials`` snapshots ``os.environ`` at construction, so every test sets env
vars *before* constructing. Config-level behaviour (SpendCaps, brand facts, auth)
lives in test_config.py and is not duplicated here.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from switchboard.credentials import Credentials, MissingCredential
from switchboard.logging_ import redact


class _FakeSMClient:
    """Stand-in for ``SecretManagerServiceClient``; records requested names."""

    def __init__(self, value=b"sm-secret-value-778899", raise_exc=False):
        self._value = value
        self._raise = raise_exc
        self.calls: list[str] = []

    def access_secret_version(self, request):
        self.calls.append(request["name"])
        if self._raise:
            raise RuntimeError("secret not found / no permission")
        return SimpleNamespace(payload=SimpleNamespace(data=self._value))


# -- primitive resolution -----------------------------------------------------

def test_resolve_present_registers_for_redaction(monkeypatch):
    # Key has no secret-hint, so only resolve(secret=True) registers it.
    monkeypatch.setenv("SBT_ALPHA", "alpha-secret-val-112233")
    creds = Credentials()
    assert creds.resolve("SBT_ALPHA") == "alpha-secret-val-112233"
    assert "alpha-secret-val-112233" not in redact("v=alpha-secret-val-112233")


def test_resolve_secret_false_not_registered(monkeypatch):
    monkeypatch.setenv("SBT_PLAIN_LABEL", "plain-config-value-abcdef")
    creds = Credentials()
    assert creds.resolve("SBT_PLAIN_LABEL", secret=False) == "plain-config-value-abcdef"
    # Not registered and not a known shape → survives redaction verbatim.
    assert redact("v=plain-config-value-abcdef") == "v=plain-config-value-abcdef"


def test_resolve_absent_returns_none(monkeypatch):
    monkeypatch.delenv("SBT_MISSING", raising=False)
    assert Credentials().resolve("SBT_MISSING") is None


def test_resolve_required_absent_raises(monkeypatch):
    monkeypatch.delenv("SBT_MISSING", raising=False)
    with pytest.raises(MissingCredential):
        Credentials().resolve("SBT_MISSING", required=True)


def test_resolve_empty_string_treated_as_absent(monkeypatch):
    monkeypatch.setenv("SBT_EMPTY", "")
    creds = Credentials()
    assert creds.resolve("SBT_EMPTY") is None
    with pytest.raises(MissingCredential):
        creds.resolve("SBT_EMPTY", required=True)


def test_has_reflects_presence(monkeypatch):
    monkeypatch.setenv("SBT_SET", "value-xyz-1")
    monkeypatch.setenv("SBT_BLANK", "")
    monkeypatch.delenv("SBT_ABSENT", raising=False)
    creds = Credentials()
    assert creds.has("SBT_SET") is True
    assert creds.has("SBT_BLANK") is False   # empty string is not present
    assert creds.has("SBT_ABSENT") is False


# -- env file vs process env --------------------------------------------------

def test_process_env_overrides_file(monkeypatch, tmp_path):
    envf = tmp_path / "a.env"
    envf.write_text("SBT_OVERRIDE=fromfile\n")
    monkeypatch.setenv("SBT_OVERRIDE", "fromenv")
    creds = Credentials(env_file=str(envf))
    assert creds.resolve("SBT_OVERRIDE", secret=False) == "fromenv"


def test_file_value_used_when_env_absent(monkeypatch, tmp_path):
    envf = tmp_path / "b.env"
    envf.write_text("SBT_FROMFILE=filevalue123\n")
    monkeypatch.delenv("SBT_FROMFILE", raising=False)
    creds = Credentials(env_file=str(envf))
    assert creds.resolve("SBT_FROMFILE", secret=False) == "filevalue123"


def test_nonexistent_env_file_is_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("SBT_X", "yval-123456")
    creds = Credentials(env_file=str(tmp_path / "nope.env"))
    assert creds.resolve("SBT_X", secret=False) == "yval-123456"


# -- prime_redaction ----------------------------------------------------------

def test_prime_redaction_registers_hinted_env(monkeypatch):
    # Name contains a secret hint ("KEY") → registered at construction, before
    # any resolve() call.
    monkeypatch.setenv("SBT_FAKE_API_KEY", "hinted-secret-aaa999")
    Credentials()
    assert "hinted-secret-aaa999" not in redact("v=hinted-secret-aaa999")


# -- Secret Manager backend (GCP mocked) --------------------------------------

def test_secrets_backend_property(monkeypatch):
    monkeypatch.delenv("SECRETS_MANAGER_PROJECT", raising=False)
    assert Credentials().secrets_backend == "env"
    monkeypatch.setenv("SECRETS_MANAGER_PROJECT", "myproj")
    assert Credentials().secrets_backend == "gcp_secret_manager"


def test_resolve_falls_back_to_secret_manager(monkeypatch):
    monkeypatch.setenv("SECRETS_MANAGER_PROJECT", "myproj")
    monkeypatch.delenv("SBT_SM_ONLY", raising=False)
    creds = Credentials()
    fake = _FakeSMClient()
    monkeypatch.setattr(creds, "_sm_client", lambda: fake)
    val = creds.resolve("SBT_SM_ONLY")
    assert val == "sm-secret-value-778899"
    # key.lower().replace('_','-') → resource id; latest version.
    assert fake.calls == ["projects/myproj/secrets/sbt-sm-only/versions/latest"]
    # resolve(secret=True) still registers the SM-sourced value for redaction.
    assert "sm-secret-value-778899" not in redact("v=sm-secret-value-778899")


def test_secret_manager_not_consulted_when_env_present(monkeypatch):
    monkeypatch.setenv("SECRETS_MANAGER_PROJECT", "myproj")
    monkeypatch.setenv("SBT_PRESENT", "env-value-abc123")
    creds = Credentials()
    fake = _FakeSMClient()
    monkeypatch.setattr(creds, "_sm_client", lambda: fake)
    assert creds.resolve("SBT_PRESENT") == "env-value-abc123"
    assert fake.calls == []  # env hit short-circuits the SM lookup


def test_secret_manager_miss_returns_none(monkeypatch):
    monkeypatch.setenv("SECRETS_MANAGER_PROJECT", "myproj")
    monkeypatch.delenv("SBT_MISS", raising=False)
    creds = Credentials()
    fake = _FakeSMClient(raise_exc=True)
    monkeypatch.setattr(creds, "_sm_client", lambda: fake)
    assert creds.resolve("SBT_MISS") is None
    with pytest.raises(MissingCredential):
        creds.resolve("SBT_MISS", required=True)


def test_secret_manager_caches_lookup(monkeypatch):
    monkeypatch.setenv("SECRETS_MANAGER_PROJECT", "myproj")
    monkeypatch.delenv("SBT_CACHED", raising=False)
    creds = Credentials()
    fake = _FakeSMClient()
    monkeypatch.setattr(creds, "_sm_client", lambda: fake)
    assert creds.resolve("SBT_CACHED") == "sm-secret-value-778899"
    assert creds.resolve("SBT_CACHED") == "sm-secret-value-778899"
    assert len(fake.calls) == 1  # second resolve served from _sm_cache


def test_secret_manager_import_error_disables_backend(monkeypatch):
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", None)
    monkeypatch.setenv("SECRETS_MANAGER_PROJECT", "myproj")
    monkeypatch.delenv("SBT_NOSDK", raising=False)
    creds = Credentials()
    assert creds.secrets_backend == "gcp_secret_manager"  # before first use
    assert creds.resolve("SBT_NOSDK") is None              # import fails → None
    assert creds._sm_project is None                        # backend disabled
    assert creds.secrets_backend == "env"


def test_secret_manager_success_via_injected_sdk(monkeypatch):
    captured: dict = {}

    class FakeSMClient:
        def __init__(self, *a, **k):
            pass

        def access_secret_version(self, request):
            captured["name"] = request["name"]
            return SimpleNamespace(payload=SimpleNamespace(data=b"injected-sm-val-334455"))

    google_mod = types.ModuleType("google")
    cloud_mod = types.ModuleType("google.cloud")
    sm_mod = types.ModuleType("google.cloud.secretmanager")
    sm_mod.SecretManagerServiceClient = FakeSMClient
    cloud_mod.secretmanager = sm_mod
    google_mod.cloud = cloud_mod
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", sm_mod)

    monkeypatch.setenv("SECRETS_MANAGER_PROJECT", "proj-x")
    monkeypatch.delenv("SBT_INJ", raising=False)
    creds = Credentials()
    # Exercises the real _sm_client (import + client construct) and _sm_get.
    assert creds.resolve("SBT_INJ") == "injected-sm-val-334455"
    assert captured["name"] == "projects/proj-x/secrets/sbt-inj/versions/latest"


# -- typed accessors + defaults -----------------------------------------------

def test_sentinel_default_account(monkeypatch):
    monkeypatch.delenv("SENTINEL_API_KEY", raising=False)
    monkeypatch.delenv("SENTINEL_ACCOUNT", raising=False)
    key, account = Credentials().sentinel()
    assert key is None
    assert account == "valnet"


def test_newsapi_key_fallback(monkeypatch):
    monkeypatch.delenv("NEWSAPI_API_KEY", raising=False)
    monkeypatch.setenv("NEWS_API_KEY", "news-fallback-key-1234")
    assert Credentials().newsapi_key() == "news-fallback-key-1234"


def test_gmail_oauth_defaults(monkeypatch):
    for k in ("GMAIL_TOKEN_URI", "GMAIL_SENDER", "GMAIL_USER"):
        monkeypatch.delenv(k, raising=False)
    g = Credentials().gmail_oauth()
    assert g.token_uri == "https://oauth2.googleapis.com/token"
    assert g.sender is None


def test_gmail_sender_falls_back_to_user(monkeypatch):
    monkeypatch.delenv("GMAIL_SENDER", raising=False)
    monkeypatch.setenv("GMAIL_USER", "me@valnetinc.com")
    assert Credentials().gmail_oauth().sender == "me@valnetinc.com"


def test_database_url_default_is_registered(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = Credentials().database_url()
    assert url.startswith("postgresql+asyncpg://")
    assert url not in redact(f"connecting to {url}")  # default url registered


def test_database_url_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    assert Credentials().database_url() == "postgresql+asyncpg://u:p@h/db"


def test_session_secret_default(monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    assert Credentials().session_secret() == "dev-only-insecure-session-secret"


def test_google_oauth_client_redirect_default(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI", raising=False)
    _cid, _csecret, redirect = Credentials().google_oauth_client()
    assert redirect == "http://localhost:8080/auth/callback"


def test_slack_bot_token_brand_override(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "default-slack-tok-000")
    monkeypatch.setenv("SLACK_BOT_TOKEN_HOTCARS", "hotcars-slack-tok-111")
    creds = Credentials()
    assert creds.slack_bot_token("hotcars") == "hotcars-slack-tok-111"
    assert creds.slack_bot_token("carbuzz") == "default-slack-tok-000"  # no per-brand → default
    assert creds.slack_bot_token() == "default-slack-tok-000"


def test_trend_agent_reads_typed_env(monkeypatch):
    monkeypatch.setenv("TREND_AGENT_ARTICLE_URL", "http://article")
    monkeypatch.setenv("TREND_AGENT_ARTICLE_TOKEN", "article-tok-222")
    url, tok = Credentials().trend_agent("article")
    assert url == "http://article"
    assert tok == "article-tok-222"


def test_google_sa_project_id_fallback_chain(monkeypatch):
    for k in ("BIGQUERY_PROJECT_ID", "BQ_PROJECT_ID",
              "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GCP_PROJECT", "gcp-proj-333")
    sa = Credentials().google_sa()
    assert sa.project_id == "gcp-proj-333"  # third fallback in the chain
    assert sa.inline_json is None
    assert sa.path is None


# -- readiness + repr ---------------------------------------------------------

def test_describe_presence_map_and_newsapi_tuple(monkeypatch):
    monkeypatch.delenv("NEWSAPI_API_KEY", raising=False)
    monkeypatch.setenv("NEWS_API_KEY", "x-newsapi-key-444")   # tuple fallback
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key-555")
    monkeypatch.delenv("ASANA_PAT", raising=False)
    d = Credentials().describe()
    assert d["anthropic"] is True
    assert d["newsapi"] is True     # matched via NEWS_API_KEY fallback
    assert d["asana"] is False
    assert all(isinstance(v, bool) for v in d.values())


def test_repr_never_leaks_values(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "super-secret-anthropic-666")
    r = repr(Credentials())
    assert "super-secret-anthropic-666" not in r
    assert "anthropic" in r          # present accessor name is shown, value is not


if __name__ == "__main__":
    import inspect

    for name, fn in sorted(globals().items()):
        if (name.startswith("test_") and callable(fn)
                and not inspect.iscoroutinefunction(fn)
                and not inspect.signature(fn).parameters):
            fn()
            print(f"PASS {name}")
