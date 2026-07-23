"""The credentials plane (PRD §8, §9.1, §11).

One place through which every integration authenticates. Rules enforced here:

* Secrets come from the credentials layer *at call time* — never hard-coded in
  adapters, prompts, memory, or logs.
* Every resolved secret is registered with :mod:`switchboard.logging_` so it is
  scrubbed from all logging.
* **User identity is kept separate from resource credentials.** The logged-in
  Google user (see auth) is used only for attribution on approvals; every tool
  call uses a service-account / app credential resolved here, never the user's
  token.

This module intentionally does **not** import any cloud SDKs — it returns the
raw material (keys, inline JSON, file paths, refresh tokens) and lets the
optional-dependency adapters construct SDK client objects. That keeps the core
installable without the heavy `data`/`ads`/`browser` extras.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

from dotenv import dotenv_values

from .logging_ import get_logger, register_secret

log = get_logger("credentials")

# Env var name fragments that indicate a value must be treated as a secret and
# registered for log redaction.
_SECRET_HINTS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PAT", "REFRESH", "WEBHOOK",
    "PRIVATE", "CREDENTIAL", "ACCESS_KEY", "SESSION", "APP_PASSWORD",
)


class MissingCredential(RuntimeError):
    """Raised when a required credential is absent from the credentials layer."""


class GoogleSA(NamedTuple):
    """A Google service-account credential, as either inline JSON or a key file
    path. Adapters build a ``google.oauth2.service_account.Credentials`` from
    whichever is populated."""
    inline_json: str | None
    path: str | None
    project_id: str | None


class GmailOAuth(NamedTuple):
    client_id: str | None
    client_secret: str | None
    refresh_token: str | None
    token_uri: str
    sender: str | None


class GoogleAdsCreds(NamedTuple):
    developer_token: str | None
    client_id: str | None
    client_secret: str | None
    refresh_token: str | None
    customer_id: str | None


class FacebookAdsCreds(NamedTuple):
    access_token: str | None
    ad_account_id: str | None


class BingAdsCreds(NamedTuple):
    developer_token: str | None
    client_id: str | None
    client_secret: str | None
    refresh_token: str | None
    customer_id: str | None
    account_id: str | None


class S3Creds(NamedTuple):
    access_key_id: str | None
    secret_access_key: str | None
    bucket: str | None


class Credentials:
    """Secret resolver over the merged (env file + process environment) namespace.

    Process environment wins over the env file, so container-injected secrets
    override the on-disk consolidated file.
    """

    def __init__(self, env_file: str | os.PathLike[str] | None = None) -> None:
        self._env_file = str(env_file) if env_file else None
        file_values: dict[str, str | None] = {}
        if self._env_file and Path(self._env_file).exists():
            file_values = dotenv_values(self._env_file)
        # Merge: file first, then process env overrides.
        self._values: dict[str, str] = {
            k: v for k, v in file_values.items() if v is not None
        }
        self._values.update({k: v for k, v in os.environ.items()})
        # Optional GCP Secret Manager backend (PRD §11). When SECRETS_MANAGER_PROJECT
        # is set, resolve() consults Secret Manager for keys absent from env (mirrors
        # mp-spend's project-id mode switch). Env/file remains the default + fallback.
        self._sm_project: str | None = self._values.get("SECRETS_MANAGER_PROJECT") or None
        self._sm_cache: dict[str, str | None] = {}
        self._sm_client_obj = None
        self.prime_redaction()

    @property
    def secrets_backend(self) -> str:
        return "gcp_secret_manager" if self._sm_project else "env"

    def _sm_client(self):
        if self._sm_client_obj is not None:
            return self._sm_client_obj
        try:
            from google.cloud import secretmanager  # type: ignore
        except ImportError:
            log.warning("google-cloud-secret-manager not installed; disabling Secret Manager backend")
            self._sm_project = None
            return None
        self._sm_client_obj = secretmanager.SecretManagerServiceClient()
        return self._sm_client_obj

    def _sm_get(self, key: str) -> str | None:
        if key in self._sm_cache:
            return self._sm_cache[key]
        result: str | None = None
        client = self._sm_client()
        if client is not None:
            try:
                name = f"projects/{self._sm_project}/secrets/{key.lower().replace('_', '-')}/versions/latest"
                resp = client.access_secret_version(request={"name": name})
                result = resp.payload.data.decode("utf-8")
            except Exception as exc:  # noqa: BLE001 — missing secret / perms → fall back to env
                log.debug("Secret Manager miss for %s: %s", key, exc)
                result = None
        self._sm_cache[key] = result
        return result

    # -- primitive resolution -------------------------------------------------

    def resolve(self, key: str, *, required: bool = False, secret: bool = True) -> str | None:
        """Return the value for ``key`` (or ``None``). Registers it for redaction
        when ``secret`` is true and the value is present."""
        val = self._values.get(key)
        if (val is None or val == "") and self._sm_project:
            val = self._sm_get(key)  # env miss → try Secret Manager
        if val is None or val == "":
            if required:
                raise MissingCredential(f"Required credential '{key}' is not set")
            return None
        if secret:
            register_secret(val)
        return val

    def has(self, key: str) -> bool:
        return bool(self._values.get(key))

    def prime_redaction(self) -> None:
        """Register every secret-looking env value up front, so redaction works
        even for values not yet resolved through a typed accessor."""
        for k, v in self._values.items():
            if v and any(hint in k.upper() for hint in _SECRET_HINTS):
                register_secret(v)

    # -- typed accessors (one per integration) --------------------------------

    def anthropic_key(self) -> str | None:
        return self.resolve("ANTHROPIC_API_KEY")

    def asana_pat(self) -> str | None:
        return self.resolve("ASANA_PAT")

    def sentinel(self) -> tuple[str | None, str]:
        return self.resolve("SENTINEL_API_KEY"), self.resolve("SENTINEL_ACCOUNT", secret=False) or "valnet"

    def ahrefs_key(self) -> str | None:
        return self.resolve("AHREFS_API_KEY")

    def similarweb_key(self) -> str | None:
        return self.resolve("SIMILARWEB_API_KEY")

    # Trend-pipeline sources (docs/trend-pipeline.md). All optional — the
    # sourcing adapters degrade softly when a key is absent.

    def tavily_key(self) -> str | None:
        return self.resolve("TAVILY_API_KEY")

    def perplexity_key(self) -> str | None:
        return self.resolve("PERPLEXITY_API_KEY")

    def firecrawl_key(self) -> str | None:
        return self.resolve("FIRECRAWL_API_KEY")

    def newsapi_key(self) -> str | None:
        return self.resolve("NEWSAPI_API_KEY") or self.resolve("NEWS_API_KEY")

    def youtube_key(self) -> str | None:
        return self.resolve("YOUTUBE_API_KEY")

    def x_bearer(self) -> str | None:
        return self.resolve("X_BEARER_TOKEN")

    def semrush_key(self) -> str | None:
        return self.resolve("SEMRUSH_API_KEY")

    def trend_agent(self, content_type: str) -> tuple[str | None, str | None]:
        """Generic ShellAgent workflow generator for a content type:
        (base_url, bearer_token) from TREND_AGENT_<TYPE>_URL / _TOKEN."""
        key = content_type.upper()
        return (
            self.resolve(f"TREND_AGENT_{key}_URL", secret=False),
            self.resolve(f"TREND_AGENT_{key}_TOKEN"),
        )

    def google_sa(self) -> GoogleSA:
        """The Google service account used for BigQuery + Sheets (read-scoped)."""
        return GoogleSA(
            inline_json=self.resolve("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON"),
            path=self.resolve("GOOGLE_APPLICATION_CREDENTIALS", secret=False),
            project_id=self.resolve("BIGQUERY_PROJECT_ID", secret=False)
            or self.resolve("BQ_PROJECT_ID", secret=False)
            or self.resolve("GCP_PROJECT", secret=False),
        )

    def gmail_oauth(self) -> GmailOAuth:
        return GmailOAuth(
            client_id=self.resolve("GMAIL_CLIENT_ID", secret=False),
            client_secret=self.resolve("GMAIL_CLIENT_SECRET"),
            refresh_token=self.resolve("GMAIL_REFRESH_TOKEN"),
            token_uri=self.resolve("GMAIL_TOKEN_URI", secret=False)
            or "https://oauth2.googleapis.com/token",
            sender=self.resolve("GMAIL_SENDER", secret=False)
            or self.resolve("GMAIL_USER", secret=False),
        )

    def google_ads(self) -> GoogleAdsCreds:
        return GoogleAdsCreds(
            developer_token=self.resolve("GOOGLE_ADS_DEVELOPER_TOKEN"),
            client_id=self.resolve("GOOGLE_ADS_CLIENT_ID", secret=False),
            client_secret=self.resolve("GOOGLE_ADS_CLIENT_SECRET"),
            refresh_token=self.resolve("GOOGLE_ADS_REFRESH_TOKEN"),
            customer_id=self.resolve("GOOGLE_ADS_CUSTOMER_ID", secret=False),
        )

    def facebook_ads(self) -> FacebookAdsCreds:
        return FacebookAdsCreds(
            access_token=self.resolve("FACEBOOK_ACCESS_TOKEN"),
            ad_account_id=self.resolve("FACEBOOK_AD_ACCOUNT_ID", secret=False),
        )

    def bing_ads(self) -> BingAdsCreds:
        return BingAdsCreds(
            developer_token=self.resolve("BING_DEVELOPER_TOKEN"),
            client_id=self.resolve("BING_CLIENT_ID", secret=False),
            client_secret=self.resolve("BING_CLIENT_SECRET"),
            refresh_token=self.resolve("BING_REFRESH_TOKEN"),
            customer_id=self.resolve("BING_CUSTOMER_ID", secret=False),
            account_id=self.resolve("BING_ACCOUNT_ID", secret=False),
        )

    def lotlinx(self) -> tuple[str | None, str | None]:
        return self.resolve("LOTLINX_CLIENT_ID", secret=False), self.resolve("LOTLINX_CLIENT_SECRET")

    def s3_quotewizard(self) -> S3Creds:
        return S3Creds(
            access_key_id=self.resolve("S3_ACCESS_KEY_ID"),
            secret_access_key=self.resolve("S3_SECRET_ACCESS_KEY"),
            bucket=self.resolve("S3_BUCKET_NAME", secret=False),
        )

    def s3_carzing(self) -> S3Creds:
        return S3Creds(
            access_key_id=self.resolve("S3_CARZING_ACCESS_KEY_ID"),
            secret_access_key=self.resolve("S3_CARZING_SECRET_ACCESS_KEY"),
            bucket=self.resolve("S3_CARZING_BUCKET_NAME", secret=False),
        )

    def slack_bot_token(self, brand: str | None = None) -> str | None:
        if brand:
            token = self.resolve(f"SLACK_BOT_TOKEN_{brand.upper()}")
            if token:
                return token
        return self.resolve("SLACK_BOT_TOKEN")

    def slack_webhook(self, brand: str) -> str | None:
        return self.resolve(f"ALBERT_SLACK_WEBHOOK_{brand.upper()}")

    def database_url(self) -> str:
        url = self.resolve("DATABASE_URL")
        if not url:
            # Local default matches docker-compose.yml.
            url = "postgresql+asyncpg://switchboard:switchboard@localhost:5432/switchboard"
            register_secret(url)
        return url

    # -- auth (user identity; separate from resource creds, PRD §9.1) ---------

    def google_oauth_client(self) -> tuple[str | None, str | None, str]:
        return (
            self.resolve("GOOGLE_OAUTH_CLIENT_ID", secret=False),
            self.resolve("GOOGLE_OAUTH_CLIENT_SECRET"),
            self.resolve("GOOGLE_OAUTH_REDIRECT_URI", secret=False)
            or "http://localhost:8080/auth/callback",
        )

    def session_secret(self) -> str:
        return self.resolve("SESSION_SECRET") or "dev-only-insecure-session-secret"

    # -- readiness (no values, only presence) ---------------------------------

    def describe(self) -> dict[str, bool]:
        """Presence map for a startup readiness log — booleans only, never values.
        A tuple value means "present if ANY of these is set" (accessor fallbacks)."""
        checks: dict[str, str | tuple[str, ...]] = {
            "anthropic": "ANTHROPIC_API_KEY",
            "asana": "ASANA_PAT",
            "sentinel": "SENTINEL_API_KEY",
            "ahrefs": "AHREFS_API_KEY",
            "similarweb": "SIMILARWEB_API_KEY",
            "tavily": "TAVILY_API_KEY",
            "perplexity": "PERPLEXITY_API_KEY",
            "firecrawl": "FIRECRAWL_API_KEY",
            "newsapi": ("NEWSAPI_API_KEY", "NEWS_API_KEY"),  # match newsapi_key()'s fallback
            "youtube": "YOUTUBE_API_KEY",
            "x": "X_BEARER_TOKEN",
            "semrush": "SEMRUSH_API_KEY",
            "copyscape": "COPYSCAPE_API_KEY",
            "copyleaks": "COPYLEAKS_API_KEY",
            "google_sa_inline": "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON",
            "google_sa_file": "GOOGLE_APPLICATION_CREDENTIALS",
            "gmail": "GMAIL_REFRESH_TOKEN",
            "google_ads": "GOOGLE_ADS_REFRESH_TOKEN",
            "facebook_ads": "FACEBOOK_ACCESS_TOKEN",
            "bing_ads": "BING_REFRESH_TOKEN",
            "lotlinx": "LOTLINX_CLIENT_SECRET",
            "s3_quotewizard": "S3_SECRET_ACCESS_KEY",
            "google_oauth": "GOOGLE_OAUTH_CLIENT_SECRET",
            "database_url": "DATABASE_URL",
        }
        return {
            name: any(self.has(v) for v in (var if isinstance(var, tuple) else (var,)))
            for name, var in checks.items()
        }

    def __repr__(self) -> str:  # never leak values
        present = sorted(k for k, v in self.describe().items() if v)
        return f"Credentials(env_file={self._env_file!r}, present={present})"
