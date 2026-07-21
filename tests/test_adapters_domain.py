"""Domain adapter tests (PRD §6, §8): actions / production / opportunity /
paid_media / analytics.

Every external boundary is mocked so nothing touches the network, a DB, or a
real SDK:

* ``httpx.AsyncClient`` — swapped for a fake driven by a ``route()`` handler.
  This covers the shared ``_http`` helper AND every client that builds its own
  ``httpx.AsyncClient`` (Ahrefs, HC-Viral, Asana-observe, Lotlinx lead feed).
  An autouse blocker points ``httpx.AsyncClient`` at a raising stub so any
  un-mocked path fails loudly instead of dialing out.
* ``BigQueryClient`` / ``SheetsClient`` / ``SentinelClient`` — the adapter
  modules import these by name, so we ``monkeypatch.setattr`` the name in the
  adapter's (or client) module namespace with a fake exposing the same async
  surface. The real (installed) google SDKs are never constructed.
* ``google-ads`` / ``facebook-business`` — NOT installed. The adapters import
  them lazily inside a threaded ``_run``; we inject fake leaf modules into
  ``sys.modules`` so the real adapter code (GAQL build, id filter, cad
  conversion, row mapping) runs against a canned SDK client.
* ``ArtifactStore`` / ``_gmail_send`` — swapped in ``actions`` so assemble/send
  actions never write bytes or call Gmail.

asyncio_mode="auto" (pyproject) → async tests need no decorator.
"""

from __future__ import annotations

import json as _json
import sys
import types
from datetime import date, timedelta

import pytest

from switchboard.adapters import actions, opportunity, paid_media, production
from switchboard.adapters.base import AdapterUnavailable
from switchboard.adapters.clients.bigquery import BQResult
from switchboard.config import Settings
from switchboard.credentials import (
    BingAdsCreds,
    FacebookAdsCreds,
    GmailOAuth,
    GoogleAdsCreds,
    GoogleSA,
)
from switchboard.db.enums import EntryType
from switchboard.interfaces import CostSpec, PlanItemView

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCreds:
    """Dict-backed stand-in for ``credentials.Credentials`` with the same typed
    accessors the domain adapters call. ``resolve`` mirrors the real contract:
    missing/empty -> None, ``required`` raises."""

    def __init__(self, **values):
        self._v = values

    def resolve(self, key, *, required=False, secret=True):
        val = self._v.get(key)
        if val in (None, ""):
            if required:
                raise RuntimeError(f"missing {key}")
            return None
        return val

    # typed accessors (mirror credentials.py) ------------------------------
    def asana_pat(self):
        return self.resolve("ASANA_PAT")

    def ahrefs_key(self):
        return self.resolve("AHREFS_API_KEY")

    def google_sa(self):
        return GoogleSA(inline_json=None, path=None, project_id="proj")

    def sentinel(self):
        return self.resolve("SENTINEL_API_KEY"), self.resolve("SENTINEL_ACCOUNT", secret=False) or "valnet"

    def gmail_oauth(self):
        return GmailOAuth(
            client_id=self.resolve("GMAIL_CLIENT_ID", secret=False),
            client_secret=self.resolve("GMAIL_CLIENT_SECRET"),
            refresh_token=self.resolve("GMAIL_REFRESH_TOKEN"),
            token_uri=self.resolve("GMAIL_TOKEN_URI", secret=False) or "https://oauth2.googleapis.com/token",
            sender=self.resolve("GMAIL_SENDER", secret=False) or self.resolve("GMAIL_USER", secret=False),
        )

    def google_ads(self):
        return GoogleAdsCreds(
            developer_token=self.resolve("GOOGLE_ADS_DEVELOPER_TOKEN"),
            client_id=self.resolve("GOOGLE_ADS_CLIENT_ID", secret=False),
            client_secret=self.resolve("GOOGLE_ADS_CLIENT_SECRET"),
            refresh_token=self.resolve("GOOGLE_ADS_REFRESH_TOKEN"),
            customer_id=self.resolve("GOOGLE_ADS_CUSTOMER_ID", secret=False),
        )

    def facebook_ads(self):
        return FacebookAdsCreds(
            access_token=self.resolve("FACEBOOK_ACCESS_TOKEN"),
            ad_account_id=self.resolve("FACEBOOK_AD_ACCOUNT_ID", secret=False),
        )

    def bing_ads(self):
        return BingAdsCreds(
            developer_token=self.resolve("BING_DEVELOPER_TOKEN"),
            client_id=self.resolve("BING_CLIENT_ID", secret=False),
            client_secret=self.resolve("BING_CLIENT_SECRET"),
            refresh_token=self.resolve("BING_REFRESH_TOKEN"),
            customer_id=self.resolve("BING_CUSTOMER_ID", secret=False),
            account_id=self.resolve("BING_ACCOUNT_ID", secret=False),
        )

    def lotlinx(self):
        return self.resolve("LOTLINX_CLIENT_ID", secret=False), self.resolve("LOTLINX_CLIENT_SECRET")


class FakeStore:
    """Records ``log_tool_call`` rows; ``query`` returns a canned result list."""

    def __init__(self, query_result=None):
        self.calls: list[dict] = []
        self.queries: list[dict] = []
        self._query_result = query_result or []

    async def log_tool_call(self, **kwargs):
        self.calls.append(kwargs)

    async def query(self, **kwargs):
        self.queries.append(kwargs)
        return list(self._query_result)


class FakeGovernor:
    """Records ``charge`` + ``within_caps`` calls; ``within_caps`` verdict is
    configurable."""

    def __init__(self, within=True):
        self.charges: list[tuple] = []
        self.cap_calls: list[tuple] = []
        self._within = within

    async def charge(self, metric, amount, agent):
        self.charges.append((metric, amount, agent))

    async def within_caps(self, metric, *, additional=0):
        self.cap_calls.append((metric, additional))
        return self._within


class FakeCtx:
    def __init__(self, creds, settings, store=None, governor=None):
        self.creds = creds
        self.settings = settings
        self.store = store or FakeStore()
        self.governor = governor or FakeGovernor()


_DEFAULT_ENDPOINTS = {
    "albert": "http://albert.test",
    "seona": "http://seona.test",
    "hc_viral_hits": "http://hcviral.test",
}


def _ctx(creds=None, *, endpoints="default", caps=None, store=None, governor=None):
    fc = creds if creds is not None else FakeCreds()
    settings = Settings(creds=fc)
    settings.endpoints = dict(_DEFAULT_ENDPOINTS) if endpoints == "default" else (endpoints or {})
    if caps is not None:
        settings.caps = caps
    return FakeCtx(fc, settings, store=store, governor=governor)


def _item(**kw):
    base = dict(assigned_agent="production", action_type="do_it", brand="hotcars", params={})
    base.update(kw)
    return PlanItemView(**base)


# ---------------------------------------------------------------------------
# httpx boundary
# ---------------------------------------------------------------------------


class FakeResp:
    def __init__(self, *, json_data=None, text="", status_code=200, error=None):
        self._json = json_data
        self.text = text
        self.status_code = status_code
        self._error = error

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    def raise_for_status(self):
        if self._error is not None:
            raise self._error


def _status_error(code=500):
    import httpx

    request = httpx.Request("GET", "https://blocked.test")
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=request, response=httpx.Response(code, request=request)
    )


def install_httpx(monkeypatch, handler):
    """Swap ``httpx.AsyncClient`` for a fake driven by ``handler(method, url,
    init_kwargs, req_kwargs) -> FakeResp``. Returns the recorded call list."""
    import httpx

    calls: list[dict] = []

    class _Client:
        def __init__(self, *args, **kwargs):
            self.init = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kw):
            calls.append({"method": "GET", "url": url, "init": self.init, **kw})
            return handler("GET", url, self.init, kw)

        async def post(self, url, **kw):
            calls.append({"method": "POST", "url": url, "init": self.init, **kw})
            return handler("POST", url, self.init, kw)

        async def request(self, method, url, **kw):
            calls.append({"method": method, "url": url, "init": self.init, **kw})
            return handler(method, url, self.init, kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return calls


def route(*routes, default=None):
    def handler(method, url, init, kw):
        for sub, resp in routes:
            if sub in url:
                return resp(kw) if callable(resp) else resp
        if default is not None:
            return default
        raise AssertionError(f"unexpected {method} {url}")

    return handler


def always(resp):
    return route(("", resp))


def call_with(calls, sub):
    for c in calls:
        if sub in c["url"]:
            return c
    raise AssertionError(f"no call to {sub!r}; saw {[c['url'] for c in calls]}")


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    import httpx

    class _Blocked:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise RuntimeError("real network blocked in tests")

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise RuntimeError("real network blocked in tests")

        async def post(self, *a, **k):
            raise RuntimeError("real network blocked in tests")

        async def request(self, *a, **k):
            raise RuntimeError("real network blocked in tests")

    monkeypatch.setattr(httpx, "AsyncClient", _Blocked)


# ---------------------------------------------------------------------------
# SDK / client injectors
# ---------------------------------------------------------------------------


def _install_bq(monkeypatch, module, *, estimate=10, results=None, results_by=None, estimate_error=None):
    cap = {"estimates": [], "queries": [], "sa": None}

    class _BQ:
        def __init__(self, sa):
            cap["sa"] = sa

        async def estimate_bytes(self, sql, params=None):
            cap["estimates"].append((sql, params))
            if estimate_error is not None:
                raise estimate_error
            return estimate(sql) if callable(estimate) else estimate

        async def query(self, sql, params=None):
            cap["queries"].append((sql, params))
            if results_by is not None:
                for sub, res in results_by:
                    if sub in sql:
                        return res
                raise AssertionError(f"no BQ result routed for sql: {sql[:60]!r}")
            return results

    monkeypatch.setattr(module, "BigQueryClient", _BQ)
    return cap


def _install_sheets(monkeypatch, target, records):
    if isinstance(target, str):
        import importlib
        target = importlib.import_module(target)
    cap = {"reads": [], "sa": None}

    class _Sheets:
        def __init__(self, sa):
            cap["sa"] = sa

        async def read_records(self, spreadsheet_id, tab, *, max_col="Z"):
            cap["reads"].append((spreadsheet_id, tab, max_col))
            return list(records)

    monkeypatch.setattr(target, "SheetsClient", _Sheets)
    return cap


def _install_sentinel(monkeypatch, module, rows):
    cap = {"api_key": None, "account": None}

    class _S:
        def __init__(self, api_key, account="valnet", *, pace_seconds=1.0):
            cap["api_key"] = api_key
            cap["account"] = account

        async def events(self, payload, *, max_pages=10):
            cap["events_payload"] = payload
            cap["events_max_pages"] = max_pages
            return list(rows)

        async def traffic(self, payload, *, max_pages=10):
            cap["traffic_payload"] = payload
            cap["traffic_max_pages"] = max_pages
            return list(rows)

    monkeypatch.setattr(module, "SentinelClient", _S)
    return cap


class _GRow:
    def __init__(self, cid, name, cost_micros, impressions, clicks):
        self.campaign = types.SimpleNamespace(id=cid, name=name)
        self.metrics = types.SimpleNamespace(
            cost_micros=cost_micros, impressions=impressions, clicks=clicks
        )


def _install_google_ads(monkeypatch, rows):
    cap = {}

    class _Svc:
        def search(self, customer_id, query):
            cap["customer_id"] = customer_id
            cap["query"] = query
            return list(rows)

    class _Client:
        @classmethod
        def load_from_dict(cls, d):
            cap["load"] = d
            return cls()

        def get_service(self, name):
            cap["service"] = name
            return _Svc()

    mod = types.ModuleType("google.ads.googleads.client")
    mod.GoogleAdsClient = _Client
    monkeypatch.setitem(sys.modules, "google.ads.googleads.client", mod)
    return cap


def _install_facebook(monkeypatch, rows):
    cap = {}

    class _Api:
        @staticmethod
        def init(access_token=None):
            cap["access_token"] = access_token

    class _Account:
        def __init__(self, acct_id):
            cap["account_id"] = acct_id

        def get_insights(self, fields=None, params=None):
            cap["fields"] = fields
            cap["params"] = params
            return list(rows)

    m1 = types.ModuleType("facebook_business.adobjects.adaccount")
    m1.AdAccount = _Account
    m2 = types.ModuleType("facebook_business.api")
    m2.FacebookAdsApi = _Api
    monkeypatch.setitem(sys.modules, "facebook_business.adobjects.adaccount", m1)
    monkeypatch.setitem(sys.modules, "facebook_business.api", m2)
    return cap


def _install_artifact(monkeypatch):
    cap = {"puts": []}

    class _AS:
        def put_text(self, *, brand, kind, ext, text, content_type="text/plain"):
            cap["puts"].append(
                {"brand": brand, "kind": kind, "ext": ext, "text": text, "content_type": content_type}
            )
            return {
                "backend": "local",
                "key": f"{brand}/{kind}/ts.{ext}",
                "uri": "file:///x",
                "content_type": content_type,
                "bytes": len(text.encode("utf-8")),
            }

    monkeypatch.setattr(actions, "ArtifactStore", _AS)
    return cap


# ===========================================================================
# actions.py — pure helpers
# ===========================================================================


def test_est_reads_cost_estimate():
    c = actions._est(_item(cost_estimate={"ahrefs_units": 5, "llm_micros": 7, "bq_bytes": 9}))
    assert c == CostSpec(ahrefs_units=5, llm_micros=7, bq_bytes=9)


def test_est_none_and_partial_default_zero():
    assert actions._est(_item(cost_estimate=None)) == CostSpec()
    assert actions._est(_item(cost_estimate={"llm_micros": 4})) == CostSpec(llm_micros=4)


def test_dry_shape():
    res = actions._dry(_item(action_type="x"), "do a thing", {"k": 1})
    assert res.ok is True and res.dry_run is True and res.action_type == "x"
    assert res.summary == "[dry-run] would do a thing"
    assert res.result_ref == {"intended": {"k": 1}}


def test_digest_html_escapes_and_counts():
    m1 = types.SimpleNamespace(id=1, payload={"kind": "<b>"}, source_system="S&S")
    m2 = types.SimpleNamespace(id=2, payload={}, source_system=None)
    html_doc = actions._digest_html("A&B", [m1, m2])
    assert "A&amp;B" in html_doc  # brand escaped
    assert "&lt;b&gt;" in html_doc and "S&amp;S" in html_doc  # cells escaped
    assert "from 2 memory metric(s)" in html_doc
    assert "None" in html_doc  # str(None) source_system for m2


# ===========================================================================
# actions.py — IdeationTriggerAdapter
# ===========================================================================


async def test_ideation_unknown_source_unavailable():
    adp = actions.IdeationTriggerAdapter(_ctx())
    with pytest.raises(AdapterUnavailable, match="unknown ideation source"):
        await adp._act(_item(params={"source": "bogus"}), dry_run=True)


async def test_ideation_dry_run_default_source():
    adp = actions.IdeationTriggerAdapter(_ctx())
    res = await adp._act(_item(brand="hotcars", params={}), dry_run=True)  # default hc_viral_hits
    assert res.dry_run is True and res.ok is True
    intended = res.result_ref["intended"]
    assert intended["source"] == "hc_viral_hits"
    assert intended["endpoint"] == "http://hcviral.test/api/pipeline/ideate"
    assert intended["brand"] == "hotcars"


async def test_ideation_live_hc_viral_posts_with_api_key(monkeypatch):
    calls = install_httpx(monkeypatch, always(FakeResp(json_data={"queued": True})))
    ctx = _ctx(FakeCreds(HC_VIRAL_HITS_API_KEY="SEKRET"))
    adp = actions.IdeationTriggerAdapter(ctx)
    res = await adp._act(
        _item(brand="hotcars", params={"source": "hc_viral_hits"},
              cost_estimate={"llm_micros": 3}),
        dry_run=False,
    )
    assert res.ok is True and res.dry_run is False
    assert res.summary == "triggered hc_viral_hits ideation"
    assert res.result_ref == {"response": {"queued": True}}
    assert res.cost == CostSpec(llm_micros=3)
    call = call_with(calls, "/api/pipeline/ideate")
    assert call["url"] == "http://hcviral.test/api/pipeline/ideate"
    assert call["params"] == {"brand": "hotcars"}
    assert call["headers"]["X-API-Key"] == "SEKRET"


async def test_ideation_live_albert_uses_path_override(monkeypatch):
    calls = install_httpx(monkeypatch, always(FakeResp(json_data={"ok": 1})))
    ctx = _ctx(FakeCreds(ALBERT_IDEATE_PATH="/custom/ideate"))
    adp = actions.IdeationTriggerAdapter(ctx)
    res = await adp._act(_item(brand="carbuzz", params={"source": "claude_albert"}), dry_run=False)
    assert res.summary == "triggered claude_albert ideation"
    call = call_with(calls, "/custom/ideate")
    assert call["url"] == "http://albert.test/custom/ideate"
    assert call["headers"] == {}  # albert route sends no api-key header


# ===========================================================================
# actions.py — AsanaTaskAdapter
# ===========================================================================


async def test_asana_task_dry_run_intended():
    ctx = _ctx(FakeCreds(ASANA_PAT="pat", ASANA_PROJECT_HOTCARS="proj-1"))
    adp = actions.AsanaTaskAdapter(ctx)
    res = await adp._act(
        _item(brand="hotcars", params={"title": "Write it", "rationale": "x" * 300}), dry_run=True
    )
    intended = res.result_ref["intended"]
    assert intended["name"] == "Write it" and intended["project"] == "proj-1"
    assert len(intended["notes"]) == 200  # notes truncated to 200


async def test_asana_task_live_missing_creds_unavailable():
    adp = actions.AsanaTaskAdapter(_ctx(FakeCreds(ASANA_PAT="pat")))  # no project
    with pytest.raises(AdapterUnavailable, match="ASANA_PAT"):
        await adp._act(_item(brand="hotcars"), dry_run=False)


async def test_asana_task_live_creates_and_extracts_gid(monkeypatch):
    calls = install_httpx(monkeypatch, always(FakeResp(json_data={"data": {"gid": "999"}})))
    ctx = _ctx(FakeCreds(ASANA_PAT="pat", ASANA_PROJECT_HOTCARS="proj-1"))
    adp = actions.AsanaTaskAdapter(ctx)
    res = await adp._act(
        _item(brand="hotcars", params={"name": "T", "notes": "N"}), dry_run=False
    )
    assert res.summary == "created Asana task 999"
    assert res.result_ref == {"task_gid": "999"}
    call = call_with(calls, "/tasks")
    assert call["url"] == "https://app.asana.com/api/1.0/tasks"
    assert call["json"] == {"data": {"name": "T", "notes": "N", "projects": ["proj-1"]}}
    assert call["headers"]["Authorization"] == "Bearer pat"


async def test_asana_task_name_and_notes_fallbacks(monkeypatch):
    install_httpx(monkeypatch, always(FakeResp(json_data={"data": {}})))
    ctx = _ctx(FakeCreds(ASANA_PAT="pat", ASANA_PROJECT_HOTCARS="p"))
    adp = actions.AsanaTaskAdapter(ctx)
    # no name/title -> default; no notes/rationale in params -> item.rationale
    res = await adp._act(_item(brand="hotcars", params={}, rationale="because"), dry_run=True)
    intended = res.result_ref["intended"]
    assert intended["name"] == "Switchboard task"
    assert intended["notes"] == "because"
    assert intended["project"] == "p"
    # gid missing in live response -> None
    res2 = await adp._act(_item(brand="hotcars", params={}), dry_run=False)
    assert res2.result_ref == {"task_gid": None}


# ===========================================================================
# actions.py — AlbertRouteToWriterAdapter / SeonaDecayRefreshAdapter
# ===========================================================================


async def test_albert_route_dry_and_live(monkeypatch):
    ctx = _ctx()
    adp = actions.AlbertRouteToWriterAdapter(ctx)
    dry = await adp._act(_item(brand="hotcars", params={"topic_id": 42}), dry_run=True)
    assert dry.result_ref["intended"] == {
        "endpoint": "http://albert.test/api/writer/route", "topic_id": 42, "brand": "hotcars"
    }
    calls = install_httpx(monkeypatch, always(FakeResp(json_data={"routed": True})))
    live = await adp._act(
        _item(brand="hotcars", params={"topic_id": 42}, cost_estimate={"llm_micros": 5}),
        dry_run=False,
    )
    assert live.summary == "routed topic 42 to writer"
    assert live.cost == CostSpec(llm_micros=5)
    assert call_with(calls, "/api/writer/route")["json"] == {"topic_id": 42, "brand": "hotcars"}


async def test_seona_decay_dry_uses_url_in_desc_and_live_merges_params(monkeypatch):
    ctx = _ctx(FakeCreds(SEONA_DECAY_PATH="/q"))
    adp = actions.SeonaDecayRefreshAdapter(ctx)
    dry = await adp._act(_item(brand="hotcars", params={"url": "http://a/x"}), dry_run=True)
    assert "http://a/x" in dry.summary
    assert dry.result_ref["intended"]["endpoint"] == "http://seona.test/q"
    calls = install_httpx(monkeypatch, always(FakeResp(json_data={"ok": 1})))
    live = await adp._act(_item(brand="hotcars", params={"url": "http://a/x"}), dry_run=False)
    assert live.summary == "queued decay refresh"
    assert call_with(calls, "/q")["json"] == {"brand": "hotcars", "url": "http://a/x"}


# ===========================================================================
# actions.py — EmakiPublishAdapter
# ===========================================================================


async def test_emaki_requires_topic_id_even_in_dry_run():
    adp = actions.EmakiPublishAdapter(_ctx())
    with pytest.raises(AdapterUnavailable, match="requires params.topic_id"):
        await adp._act(_item(brand="hotcars", params={}), dry_run=True)


async def test_emaki_dry_run_intended():
    adp = actions.EmakiPublishAdapter(_ctx())
    res = await adp._act(_item(brand="hotcars", params={"topic_id": "t7"}), dry_run=True)
    assert res.result_ref["intended"] == {
        "topic_id": "t7", "unpublished_only": True, "featured_image": False
    }


async def test_emaki_live_posts_to_publish_path(monkeypatch):
    calls = install_httpx(monkeypatch, always(FakeResp(json_data={"draft_id": 5})))
    ctx = _ctx(FakeCreds(HC_VIRAL_HITS_API_KEY="K"))
    adp = actions.EmakiPublishAdapter(ctx)
    res = await adp._act(_item(brand="hotcars", params={"topic_id": "t7"}), dry_run=False)
    assert res.summary == "pushed topic t7 to Emaki (unpublished draft)"
    call = call_with(calls, "/api/topics/t7/emaki-publish")
    assert call["url"] == "http://hcviral.test/api/topics/t7/emaki-publish"
    assert call["headers"]["X-API-Key"] == "K"


# ===========================================================================
# actions.py — DigestAssembleAdapter
# ===========================================================================


async def test_digest_assemble_dry_run():
    adp = actions.DigestAssembleAdapter(_ctx())
    res = await adp._act(_item(brand="carbuzz"), dry_run=True)
    assert res.result_ref["intended"] == {"brand": "carbuzz"}


async def test_digest_assemble_live_builds_report_entry(monkeypatch):
    cap = _install_artifact(monkeypatch)
    metrics = [
        types.SimpleNamespace(id=11, payload={"kind": "ad_spend"}, source_system="google_ads"),
        types.SimpleNamespace(id=12, payload={"kind": "sessions"}, source_system="sentinel"),
    ]
    ctx = _ctx(store=FakeStore(query_result=metrics))
    adp = actions.DigestAssembleAdapter(ctx)
    res = await adp._act(_item(brand="hotcars", cost_estimate={"llm_micros": 2}), dry_run=False)

    assert res.ok is True and res.dry_run is False
    assert res.cost == CostSpec(llm_micros=2)
    assert res.summary.startswith("assembled digest (") and res.summary.endswith("bytes)")
    assert res.result_ref["artifact_ref"]["backend"] == "local"
    (entry,) = res.entries
    assert entry.type == EntryType.REPORT and entry.brand == "hotcars"
    assert entry.source_system == "daily_reporting"
    assert entry.payload["kind"] == "daily_digest"
    assert entry.payload["metric_entries"] == [11, 12]
    # store.query scoped to METRIC entries with a freshness window + limit
    q = ctx.store.queries[0]
    assert q["types"] == [EntryType.METRIC] and q["limit"] == 30
    assert cap["puts"][0]["kind"] == "digest" and cap["puts"][0]["content_type"] == "text/html"


# ===========================================================================
# actions.py — DigestSendAdapter (Gmail)
# ===========================================================================


async def test_digest_send_dry_run_defaults_recipients_to_sender():
    ctx = _ctx(FakeCreds(GMAIL_SENDER="me@x.com"))
    adp = actions.DigestSendAdapter(ctx)
    res = await adp._act(_item(brand="hotcars", params={}), dry_run=True)
    intended = res.result_ref["intended"]
    assert intended["recipients"] == ["me@x.com"] and intended["sender"] == "me@x.com"
    assert intended["subject"].startswith("[hotcars] Daily digest")


async def test_digest_send_live_no_refresh_token_unavailable():
    ctx = _ctx(FakeCreds(GMAIL_SENDER="me@x.com"))  # no refresh token
    adp = actions.DigestSendAdapter(ctx)
    with pytest.raises(AdapterUnavailable, match="Gmail credentials not configured"):
        await adp._act(_item(brand="hotcars", params={}), dry_run=False)


async def test_digest_send_live_sends_via_gmail(monkeypatch):
    sent = {}

    async def fake_send(gmail, recipients, subject, body_html):
        sent.update(recipients=recipients, subject=subject, body=body_html, sender=gmail.sender)
        return "MSG-1"

    monkeypatch.setattr(actions, "_gmail_send", fake_send)
    ctx = _ctx(FakeCreds(GMAIL_SENDER="me@x.com", GMAIL_REFRESH_TOKEN="rt"))
    adp = actions.DigestSendAdapter(ctx)
    res = await adp._act(
        _item(brand="hotcars", params={"recipients": ["a@x.com"], "subject": "Hi",
                                       "body_html": "<p>hi</p>"}),
        dry_run=False,
    )
    assert res.summary == "sent digest email MSG-1"
    assert res.result_ref == {"message_id": "MSG-1"}
    assert sent == {"recipients": ["a@x.com"], "subject": "Hi", "body": "<p>hi</p>", "sender": "me@x.com"}


# ===========================================================================
# actions.py — NewsletterAssembleAdapter / SocialAssembleAdapter
# ===========================================================================


async def test_newsletter_dry_run_uses_default_base():
    adp = actions.NewsletterAssembleAdapter(_ctx())  # no NEWSLETTER_API_URL
    res = await adp._act(_item(brand="carbuzz"), dry_run=True)
    assert res.result_ref["intended"]["endpoint"] == "http://localhost:5200/api/newsletter/compile"


async def test_newsletter_live_builds_draft_entry(monkeypatch):
    cap = _install_artifact(monkeypatch)
    calls = install_httpx(monkeypatch, always(FakeResp(json_data={"html": "<h1>NL</h1>"})))
    ctx = _ctx(FakeCreds(NEWSLETTER_API_URL="http://nl.test"))
    adp = actions.NewsletterAssembleAdapter(ctx)
    res = await adp._act(
        _item(brand="carbuzz", params={"content": {"title": "T"}}), dry_run=False
    )
    assert res.summary == "assembled newsletter draft (HTML)"
    (entry,) = res.entries
    assert entry.type == EntryType.DISTRIBUTION_DRAFT
    assert entry.payload["kind"] == "newsletter_draft" and entry.payload["status"] == "assembled"
    assert cap["puts"][0]["text"] == "<h1>NL</h1>"
    assert call_with(calls, "/api/newsletter/compile")["json"] == {"title": "T"}


async def test_social_live_serializes_response_json(monkeypatch):
    cap = _install_artifact(monkeypatch)
    calls = install_httpx(monkeypatch, always(FakeResp(json_data={"captions": ["a", "b"]})))
    ctx = _ctx(FakeCreds(SOCIAL_API_URL="http://soc.test"))
    adp = actions.SocialAssembleAdapter(ctx)
    res = await adp._act(_item(brand="hotcars", params={"tone": "fun"}), dry_run=False)
    assert res.summary == "assembled social draft (captions)"
    (entry,) = res.entries
    assert entry.payload["kind"] == "social_draft"
    assert _json.loads(cap["puts"][0]["text"]) == {"captions": ["a", "b"]}
    assert cap["puts"][0]["ext"] == "json"
    assert call_with(calls, "/api/generate")["json"] == {"tone": "fun"}


async def test_social_dry_run_intended_includes_params():
    adp = actions.SocialAssembleAdapter(_ctx())
    res = await adp._act(_item(brand="hotcars", params={"tone": "fun"}), dry_run=True)
    assert res.result_ref["intended"]["params"] == {"tone": "fun"}
    assert res.result_ref["intended"]["endpoint"] == "http://localhost:3145/api/generate"


# ===========================================================================
# production.py — helpers + AsanaAdapter (observe)
# ===========================================================================


def test_production_today_is_iso():
    assert production._today() == date.today().isoformat()


async def test_asana_observe_no_pat_unavailable():
    adp = production.AsanaAdapter(_ctx(FakeCreds()))
    with pytest.raises(AdapterUnavailable, match="ASANA_PAT not configured"):
        await adp._observe("hotcars")


async def test_asana_observe_no_gid_unavailable():
    adp = production.AsanaAdapter(_ctx(FakeCreds(ASANA_PAT="pat")))  # no section/project gid
    with pytest.raises(AdapterUnavailable, match="No Asana GID"):
        await adp._observe("hotcars")


async def test_asana_observe_section_url_and_counts(monkeypatch):
    tasks = {
        "data": [
            {"name": "done one", "completed": True},
            {"name": "open future", "completed": False, "due_on": "2999-01-01",
             "assignee": {"name": "Al"}},
            {"name": "overdue", "completed": False, "due_on": "2000-01-01"},
        ]
    }
    calls = install_httpx(monkeypatch, always(FakeResp(json_data=tasks)))
    ctx = _ctx(FakeCreds(ASANA_PAT="pat", ASANA_SECTION_OUTLINE_HOTCARS="sec-9"))
    drafts, cost = await production.AsanaAdapter(ctx)._observe("hotcars")

    assert cost == CostSpec()
    metric = drafts[0]
    assert metric.type == EntryType.METRIC and metric.source_system == "asana"
    assert metric.payload["kind"] == "outline_queue"
    assert metric.payload["total"] == 3
    assert metric.payload["incomplete"] == 2
    assert metric.payload["overdue"] == 1
    # incomplete tasks listed with name/due/assignee
    assert metric.payload["tasks"][0]["assignee"] == "Al"
    # one overdue -> FLAG, severity medium (<=3)
    assert drafts[1].type == EntryType.FLAG
    assert drafts[1].payload == {"kind": "overdue_outlines", "count": 1, "severity": "medium"}
    # section GID -> /sections/<gid>/tasks
    call = call_with(calls, "/sections/sec-9/tasks")
    assert call["headers"]["Authorization"] == "Bearer pat"
    assert call["params"]["limit"] == 100


async def test_asana_observe_project_url_and_high_severity(monkeypatch):
    tasks = {"data": [
        {"name": f"od{i}", "completed": False, "due_on": "2000-01-01"} for i in range(4)
    ]}
    calls = install_httpx(monkeypatch, always(FakeResp(json_data=tasks)))
    ctx = _ctx(FakeCreds(ASANA_PAT="pat", ASANA_PROJECT_HOTCARS="proj-3"))
    drafts, _ = await production.AsanaAdapter(ctx)._observe("hotcars")
    assert drafts[1].payload["severity"] == "high"  # >3 overdue
    call_with(calls, "/projects/proj-3/tasks")  # falls back to project URL


async def test_asana_observe_no_overdue_no_flag(monkeypatch):
    tasks = {"data": [{"name": "future", "completed": False, "due_on": "2999-01-01"}]}
    install_httpx(monkeypatch, always(FakeResp(json_data=tasks)))
    ctx = _ctx(FakeCreds(ASANA_PAT="pat", ASANA_SECTION_OUTLINE_HOTCARS="s"))
    drafts, _ = await production.AsanaAdapter(ctx)._observe("hotcars")
    assert len(drafts) == 1  # metric only, no flag


# ===========================================================================
# production.py — HCViralDraftQueueAdapter
# ===========================================================================


async def test_hc_queue_bad_brand_unavailable():
    adp = production.HCViralDraftQueueAdapter(_ctx(FakeCreds(HC_VIRAL_HITS_API_KEY="k")))
    with pytest.raises(AdapterUnavailable, match="hotcars \\+ topspeed"):
        await adp._observe("carbuzz")


async def test_hc_queue_maps_topic_ids(monkeypatch):
    ready = [{"topic_id": "t1"}, {"id": "i2"}, {"topic_id": None, "id": "i3"}]
    calls = install_httpx(monkeypatch, always(FakeResp(json_data=ready)))
    ctx = _ctx(FakeCreds(HC_VIRAL_HITS_API_KEY="k"))
    drafts, cost = await production.HCViralDraftQueueAdapter(ctx)._observe("hotcars")
    assert cost == CostSpec()
    assert drafts[0].payload["ready_count"] == 3
    assert drafts[0].payload["ready_topic_ids"] == ["t1", "i2", "i3"]  # topic_id or id
    assert len(drafts) == 1  # <10 -> no backlog flag
    call = call_with(calls, "/api/cms/drafts")
    assert call["params"] == {"brand": "hotcars", "status": "ready"}
    assert call["headers"]["X-API-Key"] == "k"


async def test_hc_queue_backlog_flag(monkeypatch):
    ready = [{"topic_id": f"t{i}"} for i in range(10)]
    install_httpx(monkeypatch, always(FakeResp(json_data=ready)))
    ctx = _ctx(FakeCreds(HC_VIRAL_HITS_API_KEY="k"))
    drafts, _ = await production.HCViralDraftQueueAdapter(ctx)._observe("topspeed")
    assert drafts[1].type == EntryType.FLAG
    assert drafts[1].payload == {"kind": "emaki_backlog", "ready_count": 10, "severity": "medium"}


async def test_hc_queue_missing_api_key_unavailable(monkeypatch):
    # HCViralClient._headers raises AdapterUnavailable when no key -> propagates
    install_httpx(monkeypatch, always(FakeResp(json_data=[])))
    adp = production.HCViralDraftQueueAdapter(_ctx(FakeCreds()))  # no HC key
    with pytest.raises(AdapterUnavailable, match="HC_VIRAL_HITS_API_KEY"):
        await adp._observe("hotcars")


# ===========================================================================
# production.py — AlbertWriterQueueAdapter
# ===========================================================================


async def test_albert_writer_no_endpoint_unavailable():
    adp = production.AlbertWriterQueueAdapter(_ctx(endpoints={}))
    with pytest.raises(AdapterUnavailable, match="albert endpoint not configured"):
        await adp._observe("hotcars")


async def test_albert_writer_counts_states_from_list(monkeypatch):
    items = [{"state": "writing"}, {"state": "writing"}, {"state": "ready"}, {}]
    calls = install_httpx(monkeypatch, always(FakeResp(json_data=items)))
    drafts, _ = await production.AlbertWriterQueueAdapter(_ctx())._observe("hotcars")
    assert drafts[0].payload["by_state"] == {"writing": 2, "ready": 1, "unknown": 1}
    assert drafts[0].payload["total"] == 4
    assert len(drafts) == 1  # no failed -> no flag
    assert call_with(calls, "/api/writer/queue")["params"] == {"brand": "hotcars"}


async def test_albert_writer_dict_items_and_failure_flag(monkeypatch):
    data = {"items": [{"state": "failed"}, {"state": "failed"}, {"state": "ready"}]}
    install_httpx(monkeypatch, always(FakeResp(json_data=data)))
    drafts, _ = await production.AlbertWriterQueueAdapter(_ctx())._observe("hotcars")
    assert drafts[0].payload["by_state"]["failed"] == 2
    assert drafts[1].type == EntryType.FLAG
    assert drafts[1].payload == {"kind": "writer_failures", "count": 2, "severity": "high"}


# ===========================================================================
# production.py — OutlineReviewAdapter
# ===========================================================================


async def test_outline_review_no_endpoint_unavailable():
    adp = production.OutlineReviewAdapter(_ctx(endpoints={}))
    with pytest.raises(AdapterUnavailable):
        await adp._observe("hotcars")


async def test_outline_review_pending_key(monkeypatch):
    install_httpx(monkeypatch, always(FakeResp(json_data={"pending": 2, "extra": 1})))
    drafts, _ = await production.OutlineReviewAdapter(_ctx())._observe("hotcars")
    assert drafts[0].payload["pending"] == 2
    assert drafts[0].payload["detail"] == {"pending": 2, "extra": 1}
    assert len(drafts) == 1  # 2 <= 5 -> no flag


async def test_outline_review_queue_depth_fallback_and_flag(monkeypatch):
    install_httpx(monkeypatch, always(FakeResp(json_data={"queue_depth": 6})))
    drafts, _ = await production.OutlineReviewAdapter(_ctx())._observe("hotcars")
    assert drafts[0].payload["pending"] == 6  # queue_depth fallback
    assert drafts[1].payload == {"kind": "stuck_outlines", "pending": 6, "severity": "medium"}


async def test_outline_review_list_payload_uses_len(monkeypatch):
    install_httpx(monkeypatch, always(FakeResp(json_data=[{"a": 1}, {"b": 2}, {"c": 3}])))
    drafts, _ = await production.OutlineReviewAdapter(_ctx())._observe("hotcars")
    assert drafts[0].payload["pending"] == 3  # len(list)
    assert drafts[0].payload["detail"] == {}  # not a dict -> {}


# ===========================================================================
# opportunity.py — AhrefsAdapter
# ===========================================================================


async def test_ahrefs_portfolio_unavailable():
    adp = opportunity.AhrefsAdapter(_ctx())
    with pytest.raises(AdapterUnavailable, match="brand-scoped"):
        await adp._observe("portfolio")


async def test_ahrefs_cache_hit_spends_no_units():
    cached = [types.SimpleNamespace(payload={"kind": "ahrefs_cache", "target": "hotcars.com",
                                             "data": {"rows": [1]}})]
    gov = FakeGovernor()
    ctx = _ctx(FakeCreds(AHREFS_API_KEY="ak"), store=FakeStore(query_result=cached), governor=gov)
    drafts, cost = await opportunity.AhrefsAdapter(ctx)._observe("hotcars")
    assert cost == CostSpec()  # zero units on a cache hit
    assert len(drafts) == 1 and drafts[0].type == EntryType.METRIC
    assert drafts[0].payload["from_cache"] is True
    assert gov.cap_calls == []  # never checks caps when cached


async def test_ahrefs_cap_exceeded_unavailable():
    gov = FakeGovernor(within=False)
    ctx = _ctx(FakeCreds(AHREFS_API_KEY="ak"), store=FakeStore(query_result=[]), governor=gov)
    with pytest.raises(AdapterUnavailable, match="cap would be exceeded"):
        await opportunity.AhrefsAdapter(ctx)._observe("hotcars")
    assert gov.cap_calls == [("ahrefs_units", 10)]  # default units_for(1) == 10


async def test_ahrefs_missing_key_unavailable():
    # cache miss + caps ok, but no AHREFS_API_KEY -> AhrefsClient ctor raises
    ctx = _ctx(FakeCreds(), store=FakeStore(query_result=[]))
    with pytest.raises(AdapterUnavailable, match="AHREFS_API_KEY"):
        await opportunity.AhrefsAdapter(ctx)._observe("hotcars")


async def test_ahrefs_live_fetch_charges_by_rowcount(monkeypatch):
    body = {"rows": [{"kw": "a"}, {"kw": "b"}, {"kw": "c"}]}
    calls = install_httpx(monkeypatch, always(FakeResp(json_data=body)))
    ctx = _ctx(FakeCreds(AHREFS_API_KEY="ak"), store=FakeStore(query_result=[]))
    drafts, cost = await opportunity.AhrefsAdapter(ctx)._observe(
        "hotcars", date="2026-07-01", country="us", limit=3, ignored_kwarg="drop"
    )
    # 3 rows * 10 units/row
    assert cost == CostSpec(ahrefs_units=30)
    assert [d.type for d in drafts] == [EntryType.CONTEXT, EntryType.METRIC]
    assert drafts[0].payload["kind"] == "ahrefs_cache" and drafts[0].ttl_seconds == 7 * 24 * 3600
    assert drafts[1].payload["row_count"] == 3 and len(drafts[1].payload["sample"]) == 3
    assert drafts[1].payload["target"] == "hotcars.com"  # default target = brand domain
    call = call_with(calls, "api.ahrefs.com")
    assert call["params"]["target"] == "hotcars.com" and call["params"]["mode"] == "domain"
    # only whitelisted kwargs thread through; ignored_kwarg dropped
    assert call["params"]["date"] == "2026-07-01" and call["params"]["country"] == "us"
    assert "ignored_kwarg" not in call["params"]
    assert call["headers"]["Authorization"] == "Bearer ak"


async def test_ahrefs_keywords_fallback_and_empty_rows(monkeypatch):
    # no "rows"; "keywords" empty -> [data] path skipped (data falsy? no, dict truthy)
    install_httpx(monkeypatch, always(FakeResp(json_data={"keywords": []})))
    ctx = _ctx(FakeCreds(AHREFS_API_KEY="ak"), store=FakeStore(query_result=[]))
    drafts, cost = await opportunity.AhrefsAdapter(ctx)._observe("hotcars", endpoint="ep", target="t.com")
    # keywords==[] falsy, data truthy -> rows=[data] (one dict) -> units_for(1)=10
    assert cost == CostSpec(ahrefs_units=10)
    assert drafts[1].payload["row_count"] == 1


# ===========================================================================
# opportunity.py — GSCAdapter
# ===========================================================================


async def test_gsc_portfolio_unavailable():
    with pytest.raises(AdapterUnavailable, match="brand-scoped"):
        await opportunity.GSCAdapter(_ctx())._observe("portfolio")


async def test_gsc_estimate_failure_flags_unavailable(monkeypatch):
    _install_bq(monkeypatch, opportunity, estimate_error=RuntimeError("no such table xyz"))
    drafts, cost = await opportunity.GSCAdapter(_ctx())._observe("hotcars")
    assert cost == CostSpec()
    assert drafts[0].type == EntryType.FLAG
    assert drafts[0].payload["kind"] == "gsc_unavailable"
    assert drafts[0].payload["table"] == "gsc.hotcars_com_searchdata_url_impression"
    assert "no such table" in drafts[0].payload["detail"]


async def test_gsc_empty_rows_flag(monkeypatch):
    _install_bq(monkeypatch, opportunity, estimate=5, results=BQResult(rows=[], bytes_processed=123))
    drafts, cost = await opportunity.GSCAdapter(_ctx())._observe("carbuzz")
    assert cost == CostSpec(bq_bytes=123)
    assert drafts[0].payload["kind"] == "gsc_empty"


async def test_gsc_rows_metric(monkeypatch):
    rows = [{"query": "ev", "impressions": 100, "clicks": 5, "avg_position": 8.0}]
    _install_bq(monkeypatch, opportunity, estimate=5, results=BQResult(rows=rows, bytes_processed=999))
    drafts, cost = await opportunity.GSCAdapter(_ctx())._observe("hotcars")
    assert cost == CostSpec(bq_bytes=999)
    assert drafts[0].type == EntryType.METRIC
    assert drafts[0].payload["kind"] == "striking_distance" and drafts[0].payload["keywords"] == rows


# ===========================================================================
# opportunity.py — Albert/Seona ideation (shared base) + HC viral ideation
# ===========================================================================


async def test_albert_ideation_no_endpoint_unavailable():
    adp = opportunity.AlbertIdeationAdapter(_ctx(endpoints={}))
    with pytest.raises(AdapterUnavailable, match="albert endpoint not configured"):
        await adp._observe("hotcars")


async def test_albert_ideation_maps_list(monkeypatch):
    topics = [{"id": "1", "title": "T", "status": "proposed"},
              {"topic_id": "2", "headline": "H"}]
    calls = install_httpx(monkeypatch, always(FakeResp(json_data=topics)))
    drafts, cost = await opportunity.AlbertIdeationAdapter(_ctx())._observe("hotcars")
    assert cost == CostSpec()
    assert [d.payload["topic_id"] for d in drafts] == ["1", "2"]
    assert [d.payload["title"] for d in drafts] == ["T", "H"]  # title or headline
    assert drafts[0].source_system == "claude_albert" and drafts[0].payload["source"] == "claude_albert"
    assert drafts[1].payload["status"] == "proposed"  # default status
    assert drafts[0].type == EntryType.CONTEXT and drafts[0].ttl_seconds == 2 * 24 * 3600
    assert call_with(calls, "albert.test")["params"] == {"status": "proposed", "brand": "hotcars"}


async def test_albert_ideation_dict_topics_and_data_fallback(monkeypatch):
    install_httpx(monkeypatch, always(FakeResp(json_data={"topics": [{"id": "9", "title": "Z"}]})))
    drafts, _ = await opportunity.AlbertIdeationAdapter(_ctx())._observe("hotcars")
    assert drafts[0].payload["topic_id"] == "9"

    install_httpx(monkeypatch, always(FakeResp(json_data={"data": [{"id": "8", "title": "Y"}]})))
    drafts2, _ = await opportunity.AlbertIdeationAdapter(_ctx())._observe("hotcars")
    assert drafts2[0].payload["topic_id"] == "8"


async def test_albert_ideation_truncates_to_25(monkeypatch):
    topics = [{"id": str(i), "title": f"T{i}"} for i in range(30)]
    install_httpx(monkeypatch, always(FakeResp(json_data=topics)))
    drafts, _ = await opportunity.AlbertIdeationAdapter(_ctx())._observe("hotcars")
    assert len(drafts) == 25


async def test_seona_ideation_uses_own_endpoint_and_source(monkeypatch):
    calls = install_httpx(monkeypatch, always(FakeResp(json_data=[{"id": "1", "title": "T"}])))
    drafts, _ = await opportunity.SeonaIdeationAdapter(_ctx())._observe("hotcars")
    assert drafts[0].source_system == "seona" and drafts[0].payload["source"] == "seona"
    call_with(calls, "seona.test")  # hit the seona endpoint, not albert


async def test_hc_viral_ideation_bad_brand_unavailable():
    adp = opportunity.HCViralIdeationAdapter(_ctx(FakeCreds(HC_VIRAL_HITS_API_KEY="k")))
    with pytest.raises(AdapterUnavailable, match="hotcars \\+ topspeed"):
        await adp._observe("carbuzz")


async def test_hc_viral_ideation_maps(monkeypatch):
    data = [{"topic_id": "t1", "title": "A", "status": "ready"}, {"id": "i2", "title": "B"}]
    install_httpx(monkeypatch, always(FakeResp(json_data=data)))
    ctx = _ctx(FakeCreds(HC_VIRAL_HITS_API_KEY="k"))
    drafts, cost = await opportunity.HCViralIdeationAdapter(ctx)._observe("hotcars")
    assert cost == CostSpec()
    assert [d.payload["topic_id"] for d in drafts] == ["t1", "i2"]
    assert drafts[0].payload["kind"] == "viral_topic_candidate"
    assert drafts[1].payload["status"] == "ready"  # default status


# ===========================================================================
# paid_media.py — helpers
# ===========================================================================


def test_campaign_config_variants():
    assert paid_media._campaign_config(_ctx(FakeCreds())) == {}  # unset -> {}
    ctx = _ctx(FakeCreds(CAMPAIGN_CONFIG='{"google":[{"campaign_id":"1"}]}'))
    assert paid_media._campaign_config(ctx) == {"google": [{"campaign_id": "1"}]}
    bad = _ctx(FakeCreds(CAMPAIGN_CONFIG="not json{"))
    assert paid_media._campaign_config(bad) == {}  # ValueError -> {}


def test_metric_tags_domain_and_platform():
    d = paid_media._metric("hotcars", {"platform": "google_ads", "kind": "ad_spend"})
    assert d.type == EntryType.METRIC and d.brand == "hotcars"
    assert d.source_agent == "paid_media" and d.source_system == "google_ads"
    assert d.payload["domain"] == "paid_media"
    assert d.confidence == 0.9 and d.ttl_seconds == 2 * 24 * 3600
    # platform defaults to "paid_media" when absent
    assert paid_media._metric("hotcars", {"kind": "x"}).source_system == "paid_media"


# ===========================================================================
# paid_media.py — GoogleAdsAdapter (google-ads SDK injected)
# ===========================================================================


async def test_google_ads_no_refresh_token_unavailable():
    adp = paid_media.GoogleAdsAdapter(_ctx(FakeCreds()))
    with pytest.raises(AdapterUnavailable, match="Google Ads credentials"):
        await adp._observe("hotcars")


async def test_google_ads_filters_to_mp_prefix_when_no_config(monkeypatch):
    rows = [
        _GRow("1", "[CB] -M- Marketplace", 1_000_000, 500, 20),  # kept (MP prefix)
        _GRow("2", "Brand Awareness", 9_000_000, 999, 99),        # dropped (no prefix, ids==[])
    ]
    cap = _install_google_ads(monkeypatch, rows)
    ctx = _ctx(FakeCreds(GOOGLE_ADS_REFRESH_TOKEN="rt", GOOGLE_ADS_CUSTOMER_ID="123-456-7890"))
    drafts, cost = await paid_media.GoogleAdsAdapter(ctx)._observe("hotcars")

    assert cost == CostSpec()
    campaigns = drafts[0].payload["campaigns"]
    assert [c["campaign_id"] for c in campaigns] == ["1"]  # only the MP campaign
    assert campaigns[0]["spend"] == round(1.0 * paid_media._CAD_DEFAULT, 2)  # cost_micros/1e6 * default cad
    assert drafts[0].payload["total_spend_usd"] == round(1.0 * paid_media._CAD_DEFAULT, 2)
    assert drafts[0].payload["platform"] == "google_ads"
    # login_customer_id / search customer_id have dashes stripped
    assert cap["load"]["login_customer_id"] == "1234567890"
    assert cap["load"]["use_proto_plus"] is True
    assert cap["customer_id"] == "1234567890"
    assert cap["service"] == "GoogleAdsService"


async def test_google_ads_with_config_ids_includes_all_and_builds_where(monkeypatch):
    rows = [_GRow("123", "Regular Campaign", 2_000_000, 10, 1)]  # no MP prefix but ids set
    cap = _install_google_ads(monkeypatch, rows)
    ctx = _ctx(FakeCreds(GOOGLE_ADS_REFRESH_TOKEN="rt", GOOGLE_ADS_CUSTOMER_ID="55",
                         GOOGLE_CAD_TO_USD_RATE="1.0",
                         CAMPAIGN_CONFIG='{"google":[{"campaign_id":"123"}]}'))
    drafts, _ = await paid_media.GoogleAdsAdapter(ctx)._observe("hotcars")
    campaigns = drafts[0].payload["campaigns"]
    assert [c["campaign_id"] for c in campaigns] == ["123"]  # included despite no MP prefix
    assert campaigns[0]["spend"] == 2.0  # cad override 1.0
    assert "campaign.id IN (123)" in cap["query"]


async def test_google_ads_zeroed_spend_flag(monkeypatch):
    rows = [_GRow("1", "[CB] -M- Zero", 0, 100, 0)]  # MP campaign, zero cost
    _install_google_ads(monkeypatch, rows)
    ctx = _ctx(FakeCreds(GOOGLE_ADS_REFRESH_TOKEN="rt", GOOGLE_ADS_CUSTOMER_ID="1"))
    drafts, _ = await paid_media.GoogleAdsAdapter(ctx)._observe("hotcars")
    assert drafts[0].payload["total_spend_usd"] == 0
    assert drafts[1].type == EntryType.FLAG
    assert drafts[1].payload["kind"] == "zeroed_spend" and drafts[1].payload["severity"] == "medium"


# ===========================================================================
# paid_media.py — MetaAdsAdapter (facebook-business SDK injected)
# ===========================================================================


async def test_meta_ads_no_token_unavailable():
    adp = paid_media.MetaAdsAdapter(_ctx(FakeCreds()))
    with pytest.raises(AdapterUnavailable, match="Facebook Ads credentials"):
        await adp._observe("hotcars")


async def test_meta_ads_maps_rows(monkeypatch):
    rows = [{"campaign_id": "c1", "campaign_name": "Camp", "spend": "3.5",
             "impressions": "100", "inline_link_clicks": "5"}]
    cap = _install_facebook(monkeypatch, rows)
    ctx = _ctx(FakeCreds(FACEBOOK_ACCESS_TOKEN="tok", FACEBOOK_AD_ACCOUNT_ID="act_9",
                         GOOGLE_CAD_TO_USD_RATE="1.0"))
    drafts, cost = await paid_media.MetaAdsAdapter(ctx)._observe("carbuzz")
    assert cost == CostSpec()
    c = drafts[0].payload["campaigns"][0]
    assert c == {"campaign_id": "c1", "campaign_name": "Camp", "spend": 3.5,
                 "impressions": 100, "clicks": 5}
    assert drafts[0].payload["total_spend_usd"] == 3.5
    assert cap["access_token"] == "tok" and cap["account_id"] == "act_9"


async def test_meta_ads_filters_by_config_ids(monkeypatch):
    rows = [
        {"campaign_id": "c1", "spend": "1", "impressions": "1", "inline_link_clicks": "1"},
        {"campaign_id": "c2", "spend": "9", "impressions": "9", "inline_link_clicks": "9"},
    ]
    _install_facebook(monkeypatch, rows)
    ctx = _ctx(FakeCreds(FACEBOOK_ACCESS_TOKEN="tok", GOOGLE_CAD_TO_USD_RATE="1.0",
                         CAMPAIGN_CONFIG='{"facebook":[{"campaign_id":"c1"}]}'))
    drafts, _ = await paid_media.MetaAdsAdapter(ctx)._observe("carbuzz")
    ids = [c["campaign_id"] for c in drafts[0].payload["campaigns"]]
    assert ids == ["c1"]  # c2 filtered out


# ===========================================================================
# paid_media.py — BingAdsAdapter (always degrades; no SDK needed)
# ===========================================================================


async def test_bing_missing_creds_unavailable():
    adp = paid_media.BingAdsAdapter(_ctx(FakeCreds(BING_REFRESH_TOKEN="rt")))  # no dev token
    with pytest.raises(AdapterUnavailable, match="Bing Ads credentials not configured"):
        await adp._observe("carbuzz")


async def test_bing_configured_still_deferred_to_mp_spend():
    adp = paid_media.BingAdsAdapter(
        _ctx(FakeCreds(BING_REFRESH_TOKEN="rt", BING_DEVELOPER_TOKEN="dev"))
    )
    with pytest.raises(AdapterUnavailable, match="sourced from mp-spend RAW_DATA"):
        await adp._observe("carbuzz")


# ===========================================================================
# paid_media.py — SentinelEventsAdapter
# ===========================================================================


async def test_sentinel_events_no_key_unavailable():
    # real SentinelClient ctor raises when api_key is None
    adp = paid_media.SentinelEventsAdapter(_ctx(FakeCreds()))
    with pytest.raises(AdapterUnavailable, match="SENTINEL_API_KEY"):
        await adp._observe("carbuzz")


async def test_sentinel_events_aggregates_by_event(monkeypatch):
    rows = [
        {"eventName": "lotlinx_marketplace", "count": 3},
        {"eventName": "lotlinx_marketplace", "count": 2},
        {"eventName": "carzing_marketplace", "events": 4},  # 'events' count fallback
        {"count": 1},                                        # no eventName -> 'unknown'
    ]
    cap = _install_sentinel(monkeypatch, paid_media, rows)
    ctx = _ctx(FakeCreds(SENTINEL_API_KEY="sk"))
    drafts, cost = await paid_media.SentinelEventsAdapter(ctx)._observe("carbuzz")
    assert cost == CostSpec()
    by_event = drafts[0].payload["by_event"]
    assert by_event == {"lotlinx_marketplace": 5, "carzing_marketplace": 4, "unknown": 1}
    assert drafts[0].payload["platform"] == "sentinel_events"
    # default events + property/page path fed into the query filters
    filt = cap["events_payload"]["filters"]
    assert filt["eventName"]["in"] == ["lotlinx_marketplace", "carzing_marketplace",
                                       "CarsAndBids_marketplace"]
    assert filt["propertyId"]["in"] == ["www.carbuzz.com"]
    assert cap["events_max_pages"] == 5


async def test_sentinel_events_custom_events_from_env(monkeypatch):
    cap = _install_sentinel(monkeypatch, paid_media, [])
    ctx = _ctx(FakeCreds(SENTINEL_API_KEY="sk", SENTINEL_EVENT_J="ev_a", SENTINEL_EVENT_K="ev_b"))
    await paid_media.SentinelEventsAdapter(ctx)._observe("carbuzz")
    assert cap["events_payload"]["filters"]["eventName"]["in"] == ["ev_a", "ev_b"]


# ===========================================================================
# paid_media.py — LeadFeedsAdapter (httpx)
# ===========================================================================


async def test_lead_feeds_no_secret_unavailable():
    adp = paid_media.LeadFeedsAdapter(_ctx(FakeCreds(LOTLINX_CLIENT_ID="id")))  # no secret
    with pytest.raises(AdapterUnavailable, match="Lotlinx credentials"):
        await adp._observe("carbuzz")


async def test_lead_feeds_counts_valid_today(monkeypatch):
    today = date.today().isoformat()
    report = {"data": [
        {"status_label": "VALID", "create_time": f"{today}T10:00:00"},
        {"status_label": "VALID", "create_time": f"{today}T11:00:00"},
        {"status_label": "INVALID", "create_time": f"{today}T09:00:00"},  # not VALID
        {"status_label": "VALID", "create_time": "2000-01-01T00:00:00"},  # not today
    ]}
    handler = route(
        ("/auth/token", FakeResp(json_data={"token": "bear"})),
        ("/reports/click-scrub", FakeResp(json_data=report)),
    )
    calls = install_httpx(monkeypatch, handler)
    ctx = _ctx(FakeCreds(LOTLINX_CLIENT_ID="id", LOTLINX_CLIENT_SECRET="sec"))
    drafts, cost = await paid_media.LeadFeedsAdapter(ctx)._observe("carbuzz")
    assert cost == CostSpec()
    assert drafts[0].payload["valid_leads"] == 2
    assert drafts[0].payload["value_usd"] == round(2 * 0.75, 2)
    # bearer from token threaded into the report request
    assert call_with(calls, "/reports/click-scrub")["headers"]["Authorization"] == "Bearer bear"


async def test_lead_feeds_access_token_and_rows_fallback(monkeypatch):
    today = date.today().isoformat()
    handler = route(
        ("/auth/token", FakeResp(json_data={"access_token": "bear2"})),  # access_token fallback
        ("/reports/click-scrub", FakeResp(json_data={"rows": [  # 'rows' fallback
            {"status_label": "VALID", "create_time": f"{today}T01:00:00"}]})),
    )
    calls = install_httpx(monkeypatch, handler)
    ctx = _ctx(FakeCreds(LOTLINX_CLIENT_ID="id", LOTLINX_CLIENT_SECRET="sec"))
    drafts, _ = await paid_media.LeadFeedsAdapter(ctx)._observe("carbuzz")
    assert drafts[0].payload["valid_leads"] == 1
    assert call_with(calls, "/reports/click-scrub")["headers"]["Authorization"] == "Bearer bear2"


# ===========================================================================
# paid_media.py — PaidMediaSheetAdapter (Sheets; local import)
# ===========================================================================


async def test_paid_media_sheet_no_id_unavailable():
    adp = paid_media.PaidMediaSheetAdapter(_ctx(FakeCreds()))
    with pytest.raises(AdapterUnavailable, match="SPREADSHEET_ID not configured"):
        await adp._observe("carbuzz")


def _mp_date():
    y = date.today() - timedelta(days=1)
    return f"{y.month}/{y.day}/{y.year}"


async def test_paid_media_sheet_aggregates_spend_roi(monkeypatch):
    want = _mp_date()
    records = [
        {"date": want, "platform": "google", "spend_usd": "100", "lotlinx": "5",
         "carzing_sentinel": "3", "carsandbids": "2"},
        {"date": want, "platform": "meta", "spend_usd": "50.5", "lotlinx": "0",
         "carzing_sentinel": "0", "carsandbids": "0"},
        {"date": "1/1/2000", "platform": "google", "spend_usd": "999"},  # other day -> excluded
    ]
    _install_sheets(monkeypatch, "switchboard.adapters.clients.sheets", records)
    ctx = _ctx(FakeCreds(SPREADSHEET_ID="sheet-1"))
    drafts, cost = await paid_media.PaidMediaSheetAdapter(ctx)._observe("carbuzz")
    assert cost == CostSpec()
    p = drafts[0].payload
    assert p["total_spend_usd"] == 150.5 and p["total_leads"] == 10
    assert p["by_platform"] == {"google": 100.0, "meta": 50.5}
    assert p["cpl"] == round(150.5 / 10, 2) and p["row_count"] == 2
    assert len(drafts) == 1  # rows present -> no flag


async def test_paid_media_sheet_no_rows_flags_and_null_cpl(monkeypatch):
    _install_sheets(monkeypatch, "switchboard.adapters.clients.sheets",
                    [{"date": "1/1/2000", "platform": "g", "spend_usd": "5"}])
    ctx = _ctx(FakeCreds(SPREADSHEET_ID="sheet-1"))
    drafts, _ = await paid_media.PaidMediaSheetAdapter(ctx)._observe("carbuzz")
    assert drafts[0].payload["cpl"] is None and drafts[0].payload["row_count"] == 0
    assert drafts[1].type == EntryType.FLAG and drafts[1].payload["kind"] == "no_spend_rows"
