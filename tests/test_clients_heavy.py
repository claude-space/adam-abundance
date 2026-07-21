"""Heavy external-client adapter tests (PRD §4, §8; docs/trend-pipeline.md).

These five clients wrap *non-httpx* SDKs (Anthropic, google-cloud-bigquery,
google-api-python-client, google-auth) plus the Sentinel Pro httpx client. Each
imports its SDK **lazily inside the call**, so we mock at that boundary and never
open a socket:

* ``llm.py``        — patch ``anthropic.AsyncAnthropic`` (a fake async client
  whose ``messages.create`` returns a canned message/usage).
* ``bigquery.py``   — patch ``Client``/``QueryJobConfig``/``*QueryParameter`` on
  the real ``google.cloud.bigquery`` module; stub the module's
  ``build_credentials`` seam.
* ``sheets.py``     — patch ``googleapiclient.discovery.build``; stub
  ``build_credentials``.
* ``google_auth.py``— patch ``google.oauth2.service_account.Credentials``.
* ``sentinel.py``   — wrap the real ``httpx.AsyncClient`` with an
  ``httpx.MockTransport`` (same harness as test_clients_http.py) so real request
  building runs against canned responses; neutralize the retry/pace sleeps.

The "SDK not installed" branch is forced with ``sys.modules[name] = None`` (plus
deleting the stale parent attribute) so the real ``except ImportError`` path runs.

asyncio_mode="auto" (see pyproject) → async tests need no decorator.
"""

from __future__ import annotations

import json as _jsonlib
import sys
import types

import httpx
import pytest

from switchboard.adapters.base import AdapterUnavailable
from switchboard.adapters.clients import bigquery as bq_mod
from switchboard.adapters.clients import sentinel as sentinel_mod
from switchboard.adapters.clients import sheets as sheets_mod
from switchboard.adapters.clients.bigquery import BigQueryClient, BQResult
from switchboard.adapters.clients.google_auth import (
    BIGQUERY_SCOPES,
    SHEETS_SCOPES,
    build_credentials,
)
from switchboard.adapters.clients.llm import LLMClient, LLMResult
from switchboard.adapters.clients.sentinel import SentinelClient
from switchboard.adapters.clients.sheets import SheetsClient
from switchboard.credentials import GoogleSA


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
async def _anoop(*_a, **_k):
    return None


def force_import_error(monkeypatch, dotted: str) -> None:
    """Make ``import <dotted>`` raise ImportError, even if it was already
    imported for real. A ``None`` entry in ``sys.modules`` is the interpreter's
    "known-uninstallable" sentinel; we also drop the stale attribute off the
    parent package so ``from parent import child`` can't shortcut past it."""
    monkeypatch.setitem(sys.modules, dotted, None)
    if "." in dotted:
        parent, child = dotted.rsplit(".", 1)
        par = sys.modules.get(parent)
        if par is not None:
            monkeypatch.delattr(par, child, raising=False)


def install_build_credentials(monkeypatch, module, sentinel="CREDS") -> list:
    """Stub the ``build_credentials`` seam a client imported from google_auth.
    Returns the recorded ``(sa, scopes)`` call list. google_auth itself is
    exercised directly in its own section."""
    calls: list = []

    def fake(sa, scopes):
        calls.append((sa, tuple(scopes)))
        return sentinel

    monkeypatch.setattr(module, "build_credentials", fake)
    return calls


@pytest.fixture(autouse=True)
def _block_real_anthropic(monkeypatch):
    """Safety net: any LLM test that forgets ``install_anthropic`` hits this
    blocker (a loud RuntimeError) instead of dialing the real API. Tests that
    mock the client re-patch this attribute via ``install_anthropic``."""
    import anthropic

    class _BlockedMessages:
        async def create(self, **_kwargs):
            raise RuntimeError("real Anthropic API blocked in tests; call install_anthropic")

    class _Blocked:
        def __init__(self, *_a, **_k):
            self.messages = _BlockedMessages()

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Blocked)


# =========================================================================== #
# llm.py — Anthropic wrapper
# =========================================================================== #
def install_anthropic(monkeypatch, response) -> dict:
    """Patch ``anthropic.AsyncAnthropic`` with a fake whose ``messages.create``
    returns (or raises) ``response``. Captures constructor api_key + create
    kwargs + construction count."""
    import anthropic

    cap = {"api_key": None, "kwargs": None, "constructed": 0}

    class _Messages:
        async def create(self, **kwargs):
            cap["kwargs"] = kwargs
            if isinstance(response, BaseException):
                raise response
            return response

    class _FakeAsyncAnthropic:
        def __init__(self, *, api_key=None, **_kw):
            cap["api_key"] = api_key
            cap["constructed"] += 1
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAsyncAnthropic)
    return cap


def _text_block(text, citations=None):
    cites = None
    if citations is not None:
        cites = [types.SimpleNamespace(url=u) for u in citations]
    return types.SimpleNamespace(type="text", text=text, citations=cites)


def _tool_block(name, inp):
    return types.SimpleNamespace(type="tool_use", name=name, input=inp)


def _server_tool_block():
    # btype contains "server_tool_use" -> counts as a web search.
    return types.SimpleNamespace(type="server_tool_use")


def _usage(**kw):
    return types.SimpleNamespace(**kw)


def _message(content, usage=None):
    return types.SimpleNamespace(content=content, usage=usage)


class _LLMGovernor:
    def __init__(self, within=True):
        self._within = within
        self.within_calls: list = []
        self.charges: list = []

    async def within_caps(self, metric, *, additional=0):
        self.within_calls.append((metric, additional))
        return self._within

    async def charge(self, metric, amount, agent):
        self.charges.append((metric, amount, agent))


class _LLMModels:
    def __init__(self, default="claude-sonnet-4-6", factcheck="claude-haiku-4-5"):
        self.default = default
        self.factcheck = factcheck


class _LLMSettings:
    def __init__(self, models=None):
        self.models = models or _LLMModels()


class _LLMCreds:
    def __init__(self, key="sk-test"):
        self._key = key

    def anthropic_key(self):
        return self._key


class _LLMCtx:
    def __init__(self, *, key="sk-test", within=True, models=None):
        self.creds = _LLMCreds(key)
        self.settings = _LLMSettings(models)
        self.governor = _LLMGovernor(within)


async def test_llm_complete_happy_path(monkeypatch):
    resp = _message(
        [
            _text_block("Hello ", ["https://a", "https://b"]),
            _text_block("world", ["https://a"]),  # duplicate 'a' -> deduped
            _tool_block("calc", {"x": 1}),
        ],
        _usage(input_tokens=1000, output_tokens=500),
    )
    cap = install_anthropic(monkeypatch, resp)
    ctx = _LLMCtx(key="sk-xyz")
    out = await LLMClient(ctx).complete(system="SYS", prompt="P" * 30, max_tokens=256, agent="research")

    assert isinstance(out, LLMResult)
    assert out.text == "Hello world"
    assert (out.input_tokens, out.output_tokens) == (1000, 500)
    assert out.citations == ["https://a", "https://b"]  # order-preserving dedupe
    assert out.tool_uses == [{"name": "calc", "input": {"x": 1}}]
    assert out.web_search_requests == 0
    # sonnet (3,15): 1000*3 + 500*15 = 10500 micros
    assert out.micros == 10500
    assert ctx.governor.charges == [("llm_micros", 10500, "research")]

    # request shape
    assert cap["api_key"] == "sk-xyz"
    k = cap["kwargs"]
    assert k["model"] == "claude-sonnet-4-6"
    assert k["max_tokens"] == 256
    assert k["system"] == "SYS"
    assert k["messages"] == [{"role": "user", "content": "P" * 30}]
    assert "tools" not in k  # none passed
    # pre-check ran with a rough ceiling: input=len(prompt)//3=10, output=256
    assert ctx.governor.within_calls == [("llm_micros", 10 * 3 + 256 * 15)]


async def test_llm_complete_passes_tools(monkeypatch):
    cap = install_anthropic(monkeypatch, _message([_text_block("ok")], _usage(input_tokens=1, output_tokens=1)))
    tools = [{"type": "web_search_20250305", "name": "web_search"}]
    await LLMClient(_LLMCtx()).complete(system="s", prompt="p", tools=tools)
    assert cap["kwargs"]["tools"] == tools


async def test_llm_model_defaulting_and_override(monkeypatch):
    cap = install_anthropic(monkeypatch, _message([_text_block("x")], _usage()))
    ctx = _LLMCtx(models=_LLMModels(default="claude-opus-4-8"))
    await LLMClient(ctx).complete(system="s", prompt="p")
    assert cap["kwargs"]["model"] == "claude-opus-4-8"  # falls back to settings default

    cap2 = install_anthropic(monkeypatch, _message([_text_block("x")], _usage()))
    await LLMClient(ctx).complete(system="s", prompt="p", model="claude-haiku-4-5")
    assert cap2["kwargs"]["model"] == "claude-haiku-4-5"  # explicit wins


async def test_llm_cache_tokens_and_usage_web_search_costed(monkeypatch):
    resp = _message(
        [_text_block("x")],
        _usage(
            input_tokens=1000,
            output_tokens=500,
            cache_read_input_tokens=200,
            cache_creation_input_tokens=100,
            server_tool_use=_usage(web_search_requests=2),
        ),
    )
    cap = install_anthropic(monkeypatch, resp)
    ctx = _LLMCtx()
    out = await LLMClient(ctx).complete(system="s", prompt="p")

    assert out.web_search_requests == 2
    # (1000*3 + 500*15 + 100*3*1.25 + 200*3*0.10) + 2*0.01*1e6 = 10935 + 20000
    assert out.micros == 30935
    assert ctx.governor.charges[0] == ("llm_micros", 30935, "system")
    assert cap["kwargs"]["model"] == "claude-sonnet-4-6"


async def test_llm_server_tool_use_block_counts_web_search(monkeypatch):
    # No usage.server_tool_use -> web-search count comes from the blocks.
    resp = _message(
        [_text_block("x"), _server_tool_block(), _server_tool_block()],
        _usage(input_tokens=0, output_tokens=0),
    )
    install_anthropic(monkeypatch, resp)
    out = await LLMClient(_LLMCtx()).complete(system="s", prompt="p")
    assert out.web_search_requests == 2
    assert out.micros == 20000  # 2 * 1c


async def test_llm_usage_web_search_requests_overrides_block_count(monkeypatch):
    resp = _message(
        [_server_tool_block()],  # 1 from blocks...
        _usage(server_tool_use=_usage(web_search_requests=5)),  # ...overridden by usage
    )
    install_anthropic(monkeypatch, resp)
    out = await LLMClient(_LLMCtx()).complete(system="s", prompt="p")
    assert out.web_search_requests == 5


async def test_llm_zero_usage_web_search_falls_back_to_block_count(monkeypatch):
    # SUBTLE: `getattr(server, "web_search_requests", n) or n` — a reported 0 is
    # falsy and falls back to the block-counted value rather than clobbering it.
    resp = _message(
        [_server_tool_block(), _server_tool_block(), _server_tool_block()],
        _usage(server_tool_use=_usage(web_search_requests=0)),
    )
    install_anthropic(monkeypatch, resp)
    out = await LLMClient(_LLMCtx()).complete(system="s", prompt="p")
    assert out.web_search_requests == 3


async def test_llm_empty_content_and_usage(monkeypatch):
    resp = _message(None, None)  # content None, usage None
    install_anthropic(monkeypatch, resp)
    ctx = _LLMCtx()
    out = await LLMClient(ctx).complete(system="s", prompt="p")
    assert out.text == ""
    assert (out.input_tokens, out.output_tokens, out.web_search_requests) == (0, 0, 0)
    assert out.citations == [] and out.tool_uses == []
    assert out.micros == 0
    assert ctx.governor.charges == [("llm_micros", 0, "system")]  # charged even at 0


async def test_llm_text_block_without_url_citation_skipped(monkeypatch):
    # citation object with a falsy url is dropped; None citations list is fine.
    resp = _message(
        [
            types.SimpleNamespace(type="text", text="a", citations=[types.SimpleNamespace(url="")]),
            _text_block("b", None),
        ],
        _usage(input_tokens=1, output_tokens=1),
    )
    install_anthropic(monkeypatch, resp)
    out = await LLMClient(_LLMCtx()).complete(system="s", prompt="p")
    assert out.text == "ab"
    assert out.citations == []


async def test_llm_cap_exceeded_raises_before_call(monkeypatch):
    cap = install_anthropic(monkeypatch, _message([_text_block("x")], _usage()))
    ctx = _LLMCtx(within=False)
    with pytest.raises(AdapterUnavailable):
        await LLMClient(ctx).complete(system="s", prompt="p")
    assert cap["constructed"] == 0  # client never built
    assert ctx.governor.charges == []  # never charged


async def test_llm_missing_key_raises(monkeypatch):
    install_anthropic(monkeypatch, _message([_text_block("x")], _usage()))
    ctx = _LLMCtx(key=None)  # cap check passes, key missing -> unavailable
    with pytest.raises(AdapterUnavailable):
        await LLMClient(ctx).complete(system="s", prompt="p")
    assert ctx.governor.charges == []


async def test_llm_sdk_not_installed_raises(monkeypatch):
    force_import_error(monkeypatch, "anthropic")
    ctx = _LLMCtx()  # cap check passes, key present -> import is what fails
    with pytest.raises(AdapterUnavailable):
        await LLMClient(ctx).complete(system="s", prompt="p")


async def test_llm_create_error_propagates(monkeypatch):
    ctx = _LLMCtx()
    install_anthropic(monkeypatch, RuntimeError("api down"))
    with pytest.raises(RuntimeError, match="api down"):
        await LLMClient(ctx).complete(system="s", prompt="p")
    assert ctx.governor.charges == []  # error before the charge


async def test_llm_web_search_uses_factcheck_model_and_tool(monkeypatch):
    cap = install_anthropic(
        monkeypatch,
        _message([_text_block("VERIFIED: yes", ["https://src"])], _usage(input_tokens=10, output_tokens=20)),
    )
    ctx = _LLMCtx(models=_LLMModels(default="claude-sonnet-4-6", factcheck="claude-haiku-4-5"))
    out = await LLMClient(ctx).web_search("is the sky blue?")

    assert out.citations == ["https://src"]
    assert "VERIFIED" in out.text
    k = cap["kwargs"]
    assert k["model"] == "claude-haiku-4-5"  # factcheck model, not default
    assert k["max_tokens"] == 512
    assert k["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    assert "fact verification" in k["system"]
    assert ctx.governor.charges[0][2] == "research"  # default web_search agent


async def test_llm_web_search_model_override(monkeypatch):
    cap = install_anthropic(monkeypatch, _message([_text_block("x")], _usage()))
    await LLMClient(_LLMCtx()).web_search("q", model="claude-opus-4-8")
    assert cap["kwargs"]["model"] == "claude-opus-4-8"


# =========================================================================== #
# bigquery.py — google-cloud-bigquery wrapper
# =========================================================================== #
class _FakeScalarQP:
    def __init__(self, name, type_, value):
        self.name, self.type_, self.value = name, type_, value


class _FakeArrayQP:
    def __init__(self, name, type_, values):
        self.name, self.type_, self.values = name, type_, list(values)


class _FakeQJC:
    def __init__(self, *, query_parameters=None, dry_run=False, use_query_cache=True):
        self.query_parameters = query_parameters
        self.dry_run = dry_run
        self.use_query_cache = use_query_cache


class _FakeRow:
    """Mimics a BigQuery Row: exposes ``.items()``."""

    def __init__(self, d):
        self._d = d

    def items(self):
        return self._d.items()


def install_bigquery(monkeypatch, *, rows=None, total_bytes=0, query_error=None) -> dict:
    from google.cloud import bigquery as real_bq

    cap = {"clients": [], "queries": [], "job_configs": [], "result_calls": 0}

    class _Job:
        total_bytes_processed = total_bytes

        def result(self):
            cap["result_calls"] += 1
            return [_FakeRow(r) for r in (rows or [])]

    class _Client:
        def __init__(self, *, project=None, credentials=None):
            self.project = project
            self.credentials = credentials
            cap["clients"].append(self)

        def query(self, sql, job_config=None):
            cap["queries"].append(sql)
            cap["job_configs"].append(job_config)
            if query_error is not None:
                raise query_error
            return _Job()

    monkeypatch.setattr(real_bq, "Client", _Client)
    monkeypatch.setattr(real_bq, "QueryJobConfig", _FakeQJC)
    monkeypatch.setattr(real_bq, "ScalarQueryParameter", _FakeScalarQP)
    monkeypatch.setattr(real_bq, "ArrayQueryParameter", _FakeArrayQP)
    return cap


async def test_bq_query_maps_rows_bytes_and_fields(monkeypatch):
    cap = install_bigquery(monkeypatch, rows=[{"a": 1, "b": "x"}, {"a": 2, "b": "y"}], total_bytes=2048)
    bc = install_build_credentials(monkeypatch, bq_mod, sentinel="CREDS")
    sa = GoogleSA(inline_json='{"x":1}', path=None, project_id="proj-1")

    res = await BigQueryClient(sa).query("SELECT a, b FROM t")

    assert isinstance(res, BQResult)
    assert res.rows == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    assert res.fields == ["a", "b"]  # keys of the first row
    assert res.bytes_processed == 2048
    assert cap["result_calls"] == 1  # non-dry-run materializes rows

    client = cap["clients"][0]
    assert client.project == "proj-1"
    assert client.credentials == "CREDS"  # build_credentials result threaded in
    assert bc[0][0] is sa and bc[0][1] == BIGQUERY_SCOPES

    jc = cap["job_configs"][0]
    assert jc.dry_run is False and jc.use_query_cache is True


async def test_bq_query_empty_rows_empty_fields(monkeypatch):
    cap = install_bigquery(monkeypatch, rows=[], total_bytes=0)
    install_build_credentials(monkeypatch, bq_mod)
    res = await BigQueryClient(GoogleSA(None, "key.json", "proj")).query("SELECT 1")
    assert res.rows == [] and res.fields == [] and res.bytes_processed == 0


async def test_bq_scalar_param_type_inference(monkeypatch):
    cap = install_bigquery(monkeypatch, rows=[], total_bytes=0)
    install_build_credentials(monkeypatch, bq_mod)
    await BigQueryClient(GoogleSA(None, "k", "proj")).query(
        "SELECT ...", {"flag": True, "n": 5, "ratio": 1.5, "name": "abc"}
    )
    params = {p.name: (p.type_, p.value) for p in cap["job_configs"][0].query_parameters}
    # bool is checked before int (bool is an int subclass) -> BOOL, not INT64.
    assert params["flag"] == ("BOOL", True)
    assert params["n"] == ("INT64", 5)
    assert params["ratio"] == ("FLOAT64", 1.5)
    assert params["name"] == ("STRING", "abc")


async def test_bq_array_param_infers_element_type(monkeypatch):
    cap = install_bigquery(monkeypatch, rows=[], total_bytes=0)
    install_build_credentials(monkeypatch, bq_mod)
    await BigQueryClient(GoogleSA(None, "k", "proj")).query(
        "SELECT ...", {"ids": [1, 2, 3], "names": ("a", "b"), "empty": []}
    )
    by_name = {p.name: p for p in cap["job_configs"][0].query_parameters}
    assert isinstance(by_name["ids"], _FakeArrayQP)
    assert by_name["ids"].type_ == "INT64" and by_name["ids"].values == [1, 2, 3]
    assert by_name["names"].type_ == "STRING" and by_name["names"].values == ["a", "b"]  # tuple ok
    assert by_name["empty"].type_ == "STRING"  # empty seq -> default STRING


async def test_bq_estimate_bytes_dry_run(monkeypatch):
    cap = install_bigquery(monkeypatch, rows=[{"a": 1}], total_bytes=999999)
    install_build_credentials(monkeypatch, bq_mod)
    n = await BigQueryClient(GoogleSA(None, "k", "proj")).estimate_bytes("SELECT *")
    assert n == 999999
    jc = cap["job_configs"][0]
    assert jc.dry_run is True and jc.use_query_cache is False
    assert cap["result_calls"] == 0  # dry-run never calls job.result()


async def test_bq_client_is_cached_across_queries(monkeypatch):
    cap = install_bigquery(monkeypatch, rows=[], total_bytes=0)
    install_build_credentials(monkeypatch, bq_mod)
    client = BigQueryClient(GoogleSA(None, "k", "proj"))
    await client.query("SELECT 1")
    await client.query("SELECT 2")
    assert len(cap["clients"]) == 1  # constructed once, reused
    assert cap["queries"] == ["SELECT 1", "SELECT 2"]


async def test_bq_missing_project_raises(monkeypatch):
    install_bigquery(monkeypatch, rows=[], total_bytes=0)
    install_build_credentials(monkeypatch, bq_mod)  # creds resolve fine; project is what's missing
    sa = GoogleSA(inline_json='{"x":1}', path=None, project_id=None)
    with pytest.raises(AdapterUnavailable):
        await BigQueryClient(sa).query("SELECT 1")


async def test_bq_query_error_propagates(monkeypatch):
    install_bigquery(monkeypatch, query_error=RuntimeError("bq boom"))
    install_build_credentials(monkeypatch, bq_mod)
    with pytest.raises(RuntimeError, match="bq boom"):
        await BigQueryClient(GoogleSA(None, "k", "proj")).query("SELECT 1")


async def test_bq_sdk_not_installed_raises_bare_importerror(monkeypatch):
    # SURPRISING: _query_sync's own top-level `from google.cloud import bigquery`
    # is NOT wrapped, so an absent SDK surfaces as ImportError here — the soft
    # AdapterUnavailable guard in _get_client is unreachable via query().
    install_build_credentials(monkeypatch, bq_mod)
    force_import_error(monkeypatch, "google.cloud.bigquery")
    with pytest.raises(ImportError):
        await BigQueryClient(GoogleSA(None, "k", "proj")).query("SELECT 1")


# =========================================================================== #
# sheets.py — google-api-python-client (values API, read-only)
# =========================================================================== #
class _FakeExecute:
    def __init__(self, resp):
        self._resp = resp

    def execute(self):
        return self._resp


class _FakeValues:
    def __init__(self, cap, resp):
        self._cap, self._resp = cap, resp

    def get(self, *, spreadsheetId=None, range=None):
        self._cap["get_calls"].append({"spreadsheetId": spreadsheetId, "range": range})
        return _FakeExecute(self._resp)


class _FakeSpreadsheets:
    def __init__(self, cap, resp):
        self._cap, self._resp = cap, resp

    def values(self):
        return _FakeValues(self._cap, self._resp)


class _FakeService:
    def __init__(self, cap, resp):
        self._cap, self._resp = cap, resp

    def spreadsheets(self):
        return _FakeSpreadsheets(self._cap, self._resp)


def install_sheets(monkeypatch, resp) -> dict:
    from googleapiclient import discovery

    cap = {"build_calls": [], "get_calls": []}

    def fake_build(serviceName, version, *, credentials=None, cache_discovery=None, **_kw):
        cap["build_calls"].append(
            {"service": serviceName, "version": version,
             "credentials": credentials, "cache_discovery": cache_discovery}
        )
        return _FakeService(cap, resp)

    monkeypatch.setattr(discovery, "build", fake_build)
    return cap


async def test_sheets_read_returns_values_and_builds_service(monkeypatch):
    cap = install_sheets(monkeypatch, {"values": [["a", "b"], ["1", "2"]]})
    bc = install_build_credentials(monkeypatch, sheets_mod, sentinel="SC")
    sa = GoogleSA('{"x":1}', None, "proj")

    out = await SheetsClient(sa).read("SHEET_ID", "Tab!A1:B2")

    assert out == [["a", "b"], ["1", "2"]]
    b = cap["build_calls"][0]
    assert (b["service"], b["version"]) == ("sheets", "v4")
    assert b["credentials"] == "SC" and b["cache_discovery"] is False
    assert bc[0][0] is sa and bc[0][1] == SHEETS_SCOPES
    g = cap["get_calls"][0]
    assert g == {"spreadsheetId": "SHEET_ID", "range": "Tab!A1:B2"}


async def test_sheets_read_missing_values_key(monkeypatch):
    install_sheets(monkeypatch, {})  # no "values"
    install_build_credentials(monkeypatch, sheets_mod)
    out = await SheetsClient(GoogleSA(None, "k", "p")).read("id", "r")
    assert out == []


async def test_sheets_read_records_maps_headers_and_pads(monkeypatch):
    values = [["Name", " Age ", "City"], ["Alice", "30", "NYC"], ["Bob", "25"]]
    cap = install_sheets(monkeypatch, {"values": values})
    install_build_credentials(monkeypatch, sheets_mod)

    out = await SheetsClient(GoogleSA(None, "k", "p")).read_records("SID", "Sheet1")

    assert out == [
        {"Name": "Alice", "Age": "30", "City": "NYC"},
        {"Name": "Bob", "Age": "25", "City": None},  # short row padded with None
    ]
    assert cap["get_calls"][0]["range"] == "'Sheet1'!A1:Z"  # quoted tab, default max_col


async def test_sheets_read_records_extra_cells_dropped(monkeypatch):
    # A row longer than the header row: zip() truncates to header width.
    install_sheets(monkeypatch, {"values": [["A", "B"], ["1", "2", "3", "4"]]})
    install_build_credentials(monkeypatch, sheets_mod)
    out = await SheetsClient(GoogleSA(None, "k", "p")).read_records("SID", "T")
    assert out == [{"A": "1", "B": "2"}]


async def test_sheets_read_records_empty(monkeypatch):
    install_sheets(monkeypatch, {"values": []})
    install_build_credentials(monkeypatch, sheets_mod)
    out = await SheetsClient(GoogleSA(None, "k", "p")).read_records("SID", "T")
    assert out == []


async def test_sheets_read_records_custom_max_col(monkeypatch):
    cap = install_sheets(monkeypatch, {"values": [["H"]]})
    install_build_credentials(monkeypatch, sheets_mod)
    await SheetsClient(GoogleSA(None, "k", "p")).read_records("SID", "T", max_col="AA")
    assert cap["get_calls"][0]["range"] == "'T'!A1:AA"


async def test_sheets_service_is_cached(monkeypatch):
    cap = install_sheets(monkeypatch, {"values": [["a"]]})
    install_build_credentials(monkeypatch, sheets_mod)
    client = SheetsClient(GoogleSA(None, "k", "p"))
    await client.read("id", "A1:A1")
    await client.read("id", "A2:A2")
    assert len(cap["build_calls"]) == 1  # built once, reused
    assert len(cap["get_calls"]) == 2


async def test_sheets_credential_failure_propagates(monkeypatch):
    install_sheets(monkeypatch, {"values": []})

    def boom(sa, scopes):
        raise AdapterUnavailable("no cred")

    monkeypatch.setattr(sheets_mod, "build_credentials", boom)
    with pytest.raises(AdapterUnavailable):
        await SheetsClient(GoogleSA(None, None, None)).read("id", "r")


async def test_sheets_sdk_not_installed_raises(monkeypatch):
    # sheets._read_sync only touches _get_service, whose import IS guarded, so
    # the soft AdapterUnavailable path is reachable (unlike bigquery).
    install_build_credentials(monkeypatch, sheets_mod)
    force_import_error(monkeypatch, "googleapiclient.discovery")
    with pytest.raises(AdapterUnavailable):
        await SheetsClient(GoogleSA(None, "k", "p")).read("id", "r")


# =========================================================================== #
# google_auth.py — build_credentials
# =========================================================================== #
def install_service_account(monkeypatch) -> dict:
    from google.oauth2 import service_account

    cap = {"from_info": [], "from_file": []}

    class _Creds:
        @staticmethod
        def from_service_account_info(info, scopes=None):
            cap["from_info"].append({"info": info, "scopes": scopes})
            return ("CREDS_INFO", info, scopes)

        @staticmethod
        def from_service_account_file(path, scopes=None):
            cap["from_file"].append({"path": path, "scopes": scopes})
            return ("CREDS_FILE", path, scopes)

    monkeypatch.setattr(service_account, "Credentials", _Creds)
    return cap


def test_scopes_constants():
    assert BIGQUERY_SCOPES == ("https://www.googleapis.com/auth/bigquery",)
    assert SHEETS_SCOPES == ("https://www.googleapis.com/auth/spreadsheets.readonly",)


def test_build_credentials_from_inline_json(monkeypatch):
    cap = install_service_account(monkeypatch)
    sa = GoogleSA(inline_json='{"type":"service_account","x":1}', path=None, project_id="p")
    out = build_credentials(sa, BIGQUERY_SCOPES)
    assert out == ("CREDS_INFO", {"type": "service_account", "x": 1}, list(BIGQUERY_SCOPES))
    assert cap["from_info"][0]["scopes"] == list(BIGQUERY_SCOPES)  # Sequence -> list
    assert cap["from_file"] == []


def test_build_credentials_invalid_json_raises(monkeypatch):
    install_service_account(monkeypatch)
    sa = GoogleSA(inline_json="{not valid json", path=None, project_id="p")
    with pytest.raises(AdapterUnavailable):
        build_credentials(sa, SHEETS_SCOPES)


def test_build_credentials_from_file(monkeypatch):
    cap = install_service_account(monkeypatch)
    sa = GoogleSA(inline_json=None, path="/keys/sa.json", project_id="p")
    out = build_credentials(sa, SHEETS_SCOPES)
    assert out == ("CREDS_FILE", "/keys/sa.json", list(SHEETS_SCOPES))
    assert cap["from_file"][0]["path"] == "/keys/sa.json"
    assert cap["from_info"] == []


def test_build_credentials_inline_takes_precedence_over_path(monkeypatch):
    cap = install_service_account(monkeypatch)
    sa = GoogleSA(inline_json='{"a":1}', path="/k.json", project_id="p")
    out = build_credentials(sa, BIGQUERY_SCOPES)
    assert out[0] == "CREDS_INFO"
    assert cap["from_file"] == []  # path never consulted


def test_build_credentials_empty_inline_falls_through_to_path(monkeypatch):
    # inline_json="" is falsy -> the `if sa.inline_json` branch is skipped.
    cap = install_service_account(monkeypatch)
    sa = GoogleSA(inline_json="", path="/k.json", project_id="p")
    out = build_credentials(sa, SHEETS_SCOPES)
    assert out[0] == "CREDS_FILE"


def test_build_credentials_none_configured_raises(monkeypatch):
    install_service_account(monkeypatch)
    for sa in (GoogleSA(None, None, "p"), GoogleSA("", None, "p"), GoogleSA("", "", "p")):
        with pytest.raises(AdapterUnavailable):
            build_credentials(sa, BIGQUERY_SCOPES)


def test_build_credentials_sdk_not_installed_raises(monkeypatch):
    force_import_error(monkeypatch, "google.oauth2.service_account")
    sa = GoogleSA(inline_json='{"a":1}', path=None, project_id="p")
    with pytest.raises(AdapterUnavailable):
        build_credentials(sa, BIGQUERY_SCOPES)


# =========================================================================== #
# sentinel.py — Sentinel Pro httpx client
# =========================================================================== #
# Reuse test_clients_http.py's MockTransport harness so real httpx request
# building runs against canned responses (no socket opened).
_REAL_ASYNC_CLIENT = httpx.AsyncClient


class Captured:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    @property
    def count(self) -> int:
        return len(self.requests)

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "no HTTP request was made"
        return self.requests[-1]

    def param(self, name: str, idx: int = -1):
        return self.requests[idx].url.params.get(name)

    def header(self, name: str, idx: int = -1):
        return self.requests[idx].headers.get(name)


def mock_httpx(monkeypatch, responses) -> Captured:
    cap = Captured()
    real = _REAL_ASYNC_CLIENT

    if isinstance(responses, httpx.Response):
        def producer(_idx, _req, _r=responses):
            return _r
    elif callable(responses):
        def producer(_idx, req):
            return responses(req)
    else:
        seq = list(responses)

        def producer(idx, _req, _seq=seq):
            return _seq[min(idx, len(_seq) - 1)]

    def handler(request: httpx.Request) -> httpx.Response:
        idx = len(cap.requests)
        cap.requests.append(request)
        return producer(idx, request)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return cap


def jresp(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _no_sentinel_sleep(monkeypatch):
    """Neutralize both the retry backoff and the inter-page pacing sleep."""
    monkeypatch.setattr(sentinel_mod, "asyncio", types.SimpleNamespace(sleep=_anoop))


def _data(cap: Captured, idx: int = -1) -> dict:
    """Decode the JSON-encoded ``data`` query param of request ``idx``."""
    return _jsonlib.loads(cap.param("data", idx))


def test_sentinel_requires_key():
    with pytest.raises(AdapterUnavailable):
        SentinelClient(None)
    with pytest.raises(AdapterUnavailable):
        SentinelClient("")


async def test_sentinel_query_single_page(monkeypatch):
    cap = mock_httpx(monkeypatch, jresp({"data": [{"id": 1}, {"id": 2}], "totalPage": 1}))
    out = await SentinelClient("api-key").query("traffic", {"foo": "bar"})

    assert out == [{"id": 1}, {"id": 2}]
    req = cap.last
    assert req.method == "GET"
    assert req.url.host == "valnet.sentinelpro.com"  # default account
    assert req.url.path == "/api/v1/traffic/"
    assert cap.header("sentinel-api-key") == "api-key"
    assert cap.header("accept") == "application/json"
    data = _data(cap)
    assert data["foo"] == "bar"  # caller payload preserved
    assert data["pagination"] == {"pageSize": 1000, "pageNumber": 1}  # defaults injected


async def test_sentinel_custom_account_in_host(monkeypatch):
    cap = mock_httpx(monkeypatch, jresp({"data": [], "totalPage": 1}))
    await SentinelClient("k", account="hotcars").query("traffic", {})
    assert cap.last.url.host == "hotcars.sentinelpro.com"


async def test_sentinel_preserves_existing_pagesize(monkeypatch):
    cap = mock_httpx(monkeypatch, jresp({"data": [], "totalPage": 1}))
    await SentinelClient("k").query("traffic", {"pagination": {"pageSize": 50}})
    data = _data(cap)
    assert data["pagination"]["pageSize"] == 50  # setdefault leaves caller value alone
    assert data["pagination"]["pageNumber"] == 1


async def test_sentinel_paginates_and_concatenates(monkeypatch):
    _no_sentinel_sleep(monkeypatch)
    responses = [
        jresp({"data": [{"p": 1}], "totalPage": 3}),
        jresp({"data": [{"p": 2}], "totalPage": 3}),
        jresp({"data": [{"p": 3}], "totalPage": 3}),
    ]
    cap = mock_httpx(monkeypatch, responses)
    out = await SentinelClient("k").query("events", {})

    assert out == [{"p": 1}, {"p": 2}, {"p": 3}]
    assert cap.count == 3
    assert [_data(cap, i)["pagination"]["pageNumber"] for i in range(3)] == [1, 2, 3]


async def test_sentinel_stops_at_total_pages(monkeypatch):
    cap = mock_httpx(monkeypatch, jresp({"data": [{"x": 1}], "totalPage": 1}))
    out = await SentinelClient("k").query("traffic", {}, max_pages=10)
    assert out == [{"x": 1}] and cap.count == 1  # totalPage reached


async def test_sentinel_stops_on_empty_batch(monkeypatch):
    _no_sentinel_sleep(monkeypatch)
    responses = [
        jresp({"data": [{"x": 1}], "totalPage": 5}),
        jresp({"data": [], "totalPage": 5}),  # empty batch short-circuits
    ]
    cap = mock_httpx(monkeypatch, responses)
    out = await SentinelClient("k").query("traffic", {})
    assert out == [{"x": 1}] and cap.count == 2


async def test_sentinel_respects_max_pages(monkeypatch):
    _no_sentinel_sleep(monkeypatch)
    responses = [jresp({"data": [{"x": i}], "totalPage": 100}) for i in range(3)]
    cap = mock_httpx(monkeypatch, responses)
    out = await SentinelClient("k").query("traffic", {}, max_pages=2)
    assert cap.count == 2 and len(out) == 2


async def test_sentinel_retries_on_503_then_succeeds(monkeypatch):
    _no_sentinel_sleep(monkeypatch)
    cap = mock_httpx(monkeypatch, [jresp({}, status=503), jresp({"data": [{"ok": 1}], "totalPage": 1})])
    out = await SentinelClient("k").query("traffic", {})
    assert out == [{"ok": 1}] and cap.count == 2  # one retry


async def test_sentinel_retry_exhausted_raises_runtimeerror(monkeypatch):
    # SURPRISING: a persistently retryable status (500/503/...) never reaches
    # raise_for_status; after 4 attempts it raises a bare RuntimeError, not an
    # httpx.HTTPStatusError like the other clients.
    _no_sentinel_sleep(monkeypatch)
    cap = mock_httpx(monkeypatch, jresp({}, status=500))
    with pytest.raises(RuntimeError):
        await SentinelClient("k").query("traffic", {})
    assert cap.count == 4  # range(4) attempts


async def test_sentinel_non_retryable_status_raises_httpstatuserror(monkeypatch):
    _no_sentinel_sleep(monkeypatch)
    cap = mock_httpx(monkeypatch, jresp({"error": "nope"}, status=404))
    with pytest.raises(httpx.HTTPStatusError):
        await SentinelClient("k").query("traffic", {})
    assert cap.count == 1  # 404 not retryable -> immediate raise_for_status


async def test_sentinel_traffic_and_events_wrappers(monkeypatch):
    cap = mock_httpx(monkeypatch, jresp({"data": [{"a": 1}], "totalPage": 1}))
    assert await SentinelClient("k").traffic({"q": 1}) == [{"a": 1}]
    assert cap.last.url.path == "/api/v1/traffic/"

    cap2 = mock_httpx(monkeypatch, jresp({"data": [{"b": 2}], "totalPage": 1}))
    assert await SentinelClient("k").events({"q": 2}) == [{"b": 2}]
    assert cap2.last.url.path == "/api/v1/events/"


async def test_sentinel_sdk_not_installed_raises(monkeypatch):
    force_import_error(monkeypatch, "httpx")
    with pytest.raises(AdapterUnavailable):
        await SentinelClient("k").query("traffic", {})
