"""Adapter framework unit tests (PRD §8, §10): base observe/act contract, the
owner registry, the dummy read adapter, and the shared HTTP-JSON helper.

All network is mocked at the httpx boundary — ``httpx.AsyncClient`` is swapped for
a factory backed by ``httpx.MockTransport`` so real request-building, status
handling, and JSON parsing run, but nothing ever leaves the process. Backoff
sleeps are stubbed so retry timing is asserted without wall-clock waits.

Dependency-light: the base/registry/dummy tests use plain fakes for
``ctx.store`` / ``ctx.governor`` (no DB), so the whole file runs without Postgres.
"""

from __future__ import annotations

import json as _json

import httpx
import pytest

from switchboard.adapters import _http, registry
from switchboard.adapters.base import AdapterUnavailable, BaseAdapter, _loggable
from switchboard.adapters.dummy import DummyAdapter
from switchboard.db.enums import EntryType, ToolAction
from switchboard.interfaces import ActionResult, CostSpec, EntryDraft, PlanItemView


# ---------------------------------------------------------------------------
# Fakes + fixtures-as-helpers
# ---------------------------------------------------------------------------


class FakeStore:
    """Records every ``log_tool_call`` kwargs dict instead of writing a row."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def log_tool_call(self, **kwargs) -> None:
        self.calls.append(kwargs)


class FakeGovernor:
    """Records ``charge(metric, amount, agent)`` tuples."""

    def __init__(self) -> None:
        self.charges: list[tuple[str, int, str]] = []

    async def charge(self, metric: str, amount: int, agent: str) -> None:
        self.charges.append((metric, amount, agent))


class FakeCtx:
    """Enough of a RunContext for BaseAdapter: only ``.store`` and ``.governor``."""

    def __init__(self) -> None:
        self.store = FakeStore()
        self.governor = FakeGovernor()


def _ctx() -> FakeCtx:
    return FakeCtx()


def _item(**kw) -> PlanItemView:
    base = dict(id=7, assigned_agent="production", action_type="do_it",
                params={"p": 1}, brand="carbuzz")
    base.update(kw)
    return PlanItemView(**base)


# -- concrete adapters used across the base tests ---------------------------


class ReadAdapter(BaseAdapter):
    """A minimal read adapter returning one draft and a configurable cost."""

    name = "reader"
    owner_agent = "analytics"
    source_system = "rdr"

    def __init__(self, ctx, cost: CostSpec) -> None:
        super().__init__(ctx)
        self._cost = cost

    async def _observe(self, brand, **kwargs):
        draft = EntryDraft(type=EntryType.METRIC, brand=brand,
                           source_agent="analytics", payload={"k": 1})
        return [draft], self._cost


class UnavailableAdapter(BaseAdapter):
    name = "unav"

    async def _observe(self, brand, **kwargs):
        raise AdapterUnavailable("no creds")


class BoomReadAdapter(BaseAdapter):
    name = "boomread"

    async def _observe(self, brand, **kwargs):
        raise ValueError("kaboom")


class ActAdapter(BaseAdapter):
    """Action adapter that honors dry_run and reports a (non-charged) cost."""

    name = "actor"
    owner_agent = "production"

    async def _act(self, item, *, dry_run):
        return ActionResult(ok=True, dry_run=dry_run, action_type=item.action_type,
                            summary="did it", cost=CostSpec(llm_micros=99))


class ForcingActAdapter(BaseAdapter):
    """_act ignores the requested dry_run and always reports dry_run=False."""

    name = "forcing"
    owner_agent = "production"

    async def _act(self, item, *, dry_run):
        return ActionResult(ok=True, dry_run=False, action_type=item.action_type)


class BoomActAdapter(BaseAdapter):
    name = "boomact"
    owner_agent = "production"

    async def _act(self, item, *, dry_run):
        raise RuntimeError("api down")


# ---------------------------------------------------------------------------
# _loggable predicate
# ---------------------------------------------------------------------------


def test_loggable_keeps_scalars_drops_containers():
    for scalar in ("s", 3, 1.5, True, False, None):
        assert _loggable(scalar) is True
    for bulky in ([1, 2], {"a": 1}, (1,), object(), {1, 2}):
        assert _loggable(bulky) is False


# ---------------------------------------------------------------------------
# can_act — structural read-only proof
# ---------------------------------------------------------------------------


def test_base_adapter_itself_cannot_act():
    assert BaseAdapter(_ctx()).can_act is False


def test_read_only_adapter_cannot_act():
    # No _act override anywhere in the MRO -> structurally read-only.
    assert DummyAdapter(_ctx()).can_act is False
    assert ReadAdapter(_ctx(), CostSpec()).can_act is False


def test_action_adapter_can_act():
    assert ActAdapter(_ctx()).can_act is True


# ---------------------------------------------------------------------------
# observe (read path)
# ---------------------------------------------------------------------------


async def test_observe_returns_drafts_and_logs_read_row():
    ctx = _ctx()
    drafts = await DummyAdapter(ctx).observe("hotcars")
    assert len(drafts) == 1 and isinstance(drafts[0], EntryDraft)

    assert len(ctx.store.calls) == 1
    row = ctx.store.calls[0]
    assert row["agent"] == "system"
    assert row["tool"] == "dummy"
    assert row["action"] == ToolAction.READ.value == "read"
    assert row["dry_run"] is False
    assert row["ok"] is True
    assert row["brand"] == "hotcars"
    assert row["request"] == {"brand": "hotcars"}
    assert isinstance(row["cost"], CostSpec)


async def test_observe_no_charge_when_cost_zero():
    ctx = _ctx()
    await DummyAdapter(ctx).observe("hotcars")  # DummyAdapter returns CostSpec()
    assert ctx.governor.charges == []


async def test_observe_charges_only_positive_metrics_with_owner_agent():
    ctx = _ctx()
    cost = CostSpec(ahrefs_units=5, llm_micros=0, bq_bytes=42)
    await ReadAdapter(ctx, cost).observe("hc")
    # llm_micros == 0 is skipped; the other two charge against the OWNER agent.
    assert ctx.governor.charges == [
        ("ahrefs_units", 5, "analytics"),
        ("bq_bytes", 42, "analytics"),
    ]
    # The full cost (including the zeroed metric) is still logged on the row.
    assert ctx.store.calls[0]["cost"] == cost


async def test_observe_request_drops_unloggable_kwargs():
    ctx = _ctx()
    await ReadAdapter(ctx, CostSpec()).observe(
        "hc", limit=10, flag=True, note=None, ratio=1.5,
        bulky=[1, 2, 3], obj={"x": 1},
    )
    # Only brand + scalar kwargs survive into the logged request.
    assert ctx.store.calls[0]["request"] == {
        "brand": "hc", "limit": 10, "flag": True, "note": None, "ratio": 1.5,
    }


async def test_observe_unavailable_soft_fails():
    ctx = _ctx()
    result = await UnavailableAdapter(ctx).observe("brandx")
    assert result == []  # degrades to empty, never raises
    row = ctx.store.calls[0]
    assert row["ok"] is False
    assert row["dry_run"] is True  # unavailable rows are logged as dry-run
    assert row["action"] == "read"
    assert row["request"] == {"brand": "brandx", "unavailable": "no creds"}
    assert ctx.governor.charges == []  # nothing charged on failure


async def test_observe_generic_exception_is_isolated():
    ctx = _ctx()
    result = await BoomReadAdapter(ctx).observe("brandx")
    assert result == []  # a crashing adapter never crashes the observe pass
    row = ctx.store.calls[0]
    assert row["ok"] is False
    assert row["dry_run"] is True
    assert row["request"] == {"brand": "brandx", "error": "kaboom"}
    assert ctx.governor.charges == []


async def test_base_observe_without_read_surface_degrades():
    # BaseAdapter._observe raises NotImplementedError, which the generic handler
    # catches: empty result + a logged failure whose error names the missing surface.
    ctx = _ctx()
    result = await BaseAdapter(ctx).observe("brandx")
    assert result == []
    row = ctx.store.calls[0]
    assert row["ok"] is False
    assert "no read surface" in row["request"]["error"]


# ---------------------------------------------------------------------------
# act (side-effect path)
# ---------------------------------------------------------------------------


async def test_act_on_read_only_adapter_raises_and_logs_nothing():
    ctx = _ctx()
    with pytest.raises(AdapterUnavailable, match="read-only"):
        await DummyAdapter(ctx).act(_item(), dry_run=True)
    assert ctx.store.calls == []  # rejected before any log row is written


async def test_act_dry_run_success_logs_and_does_not_charge():
    ctx = _ctx()
    res = await ActAdapter(ctx).act(_item(), dry_run=True)
    assert res.ok is True and res.dry_run is True and res.action_type == "do_it"

    row = ctx.store.calls[0]
    assert row["action"] == ToolAction.ACT.value == "act"
    assert row["dry_run"] is True
    assert row["ok"] is True
    assert row["brand"] == "carbuzz"
    assert row["request"] == {"action_type": "do_it", "params": {"p": 1}, "plan_item_id": 7}
    assert row["cost"] == CostSpec(llm_micros=99)
    # act() never charges the governor — that is the dispatcher's responsibility.
    assert ctx.governor.charges == []


async def test_act_live_success():
    ctx = _ctx()
    res = await ActAdapter(ctx).act(_item(), dry_run=False)
    assert res.ok is True and res.dry_run is False
    assert ctx.store.calls[0]["dry_run"] is False


async def test_act_success_row_reflects_result_dry_run_not_requested():
    # Success rows log result.dry_run — so an adapter that downgrades to log-only
    # is recorded as dry_run even though live was requested.
    ctx = _ctx()
    res = await ForcingActAdapter(ctx).act(_item(), dry_run=True)
    assert res.dry_run is False
    assert ctx.store.calls[0]["dry_run"] is False


async def test_act_exception_returns_failed_result_and_isolates():
    ctx = _ctx()
    res = await BoomActAdapter(ctx).act(_item(), dry_run=False)
    assert res.ok is False
    assert res.error == "api down"
    assert res.action_type == "do_it"
    assert res.dry_run is False  # mirrors the requested dry_run
    row = ctx.store.calls[0]
    assert row["ok"] is False
    assert row["dry_run"] is False  # error rows log the REQUESTED dry_run
    assert row["request"]["error"] == "api down"


async def test_base_default_hooks_raise_not_implemented():
    # Both default hooks raise; _act is only reachable directly (act() gates on
    # can_act first), so exercise it here to lock the contract.
    base = BaseAdapter(_ctx())
    with pytest.raises(NotImplementedError, match="no read surface"):
        await base._observe("brandx")
    with pytest.raises(NotImplementedError, match="no action surface"):
        await base._act(_item(), dry_run=True)


# ---------------------------------------------------------------------------
# DummyAdapter
# ---------------------------------------------------------------------------


async def test_dummy_observe_draft_shape():
    ctx = _ctx()
    (draft,) = await DummyAdapter(ctx).observe("hotcars")
    assert draft.type == EntryType.METRIC
    assert draft.brand == "hotcars"
    assert draft.source_agent == "system"
    assert draft.source_system == "dummy"
    assert draft.confidence == 1.0
    assert draft.ttl_seconds == 3600
    assert draft.payload == {"kind": "healthcheck", "value": 1, "note": "dummy adapter observe()"}


def test_dummy_class_attributes():
    assert DummyAdapter.name == "dummy"
    assert DummyAdapter.source_system == "dummy"
    assert DummyAdapter.owner_agent == "system"


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_build_adapters_returns_owned_instances_in_order():
    ctx = _ctx()
    built = registry.build_adapters("research", ctx)
    assert [type(a) for a in built] == registry.REGISTRY["research"]
    assert all(isinstance(a, BaseAdapter) for a in built)
    assert all(a.ctx is ctx for a in built)  # every adapter shares the run context


def test_build_adapters_empty_for_registered_empty_agent():
    # "reporting" is registered but owns no Phase-1 read adapters.
    assert registry.build_adapters("reporting", _ctx()) == []


def test_build_adapters_unknown_agent_returns_empty_no_error():
    # Unknown names degrade to [] via REGISTRY.get(..., []) — they do NOT raise.
    assert registry.build_adapters("does-not-exist", _ctx()) == []


def test_owned_tool_names_matches_registry_and_unknown_is_empty():
    assert registry.owned_tool_names("opportunity") == [
        c.name for c in registry.REGISTRY["opportunity"]
    ]
    assert registry.owned_tool_names("does-not-exist") == []


def test_build_action_adapter_known_action():
    ctx = _ctx()
    adapter = registry.build_action_adapter("create_asana_task", ctx)
    assert type(adapter) is registry.ACTION_REGISTRY["create_asana_task"]
    assert adapter.ctx is ctx


def test_build_action_adapter_unknown_returns_none():
    assert registry.build_action_adapter("no-such-action", _ctx()) is None


def test_registry_has_no_adapter_overlap():
    # PRD no-overlap rule: each read adapter class appears under exactly one owner.
    owner_of: dict[type, str] = {}
    for agent, classes in registry.REGISTRY.items():
        for cls in classes:
            assert cls not in owner_of, f"{cls.__name__} owned by {owner_of.get(cls)} and {agent}"
            owner_of[cls] = agent


def test_all_registered_classes_are_base_adapters():
    for classes in registry.REGISTRY.values():
        assert all(issubclass(c, BaseAdapter) for c in classes)
    for cls in registry.ACTION_REGISTRY.values():
        assert issubclass(cls, BaseAdapter)


def test_all_action_registry_adapters_have_action_surface():
    # Anything dispatchable as an action must define _act (can_act True).
    for action_type, cls in registry.ACTION_REGISTRY.items():
        assert cls(_ctx()).can_act is True, f"{action_type} -> {cls.__name__} has no _act"


# ---------------------------------------------------------------------------
# _http helper — network mocked at the httpx boundary
# ---------------------------------------------------------------------------


def _install_mock_httpx(monkeypatch, responder):
    """Swap httpx.AsyncClient for a factory whose transport routes every request
    to ``responder(request) -> Response`` (or raises). Returns a log dict with the
    constructor kwargs (``ctor``) and the captured ``requests``.
    """
    real_cls = httpx.AsyncClient
    log: dict = {"ctor": [], "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        log["requests"].append(request)
        return responder(request)

    def factory(*args, **kwargs):
        log["ctor"].append(dict(kwargs))
        kwargs = dict(kwargs)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return log


def _record_sleeps(monkeypatch):
    """Stub asyncio.sleep (as seen by _http) to capture backoff delays, no waits."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(_http.asyncio, "sleep", fake_sleep)
    return sleeps


async def test_get_json_builds_request_and_parses_response(monkeypatch):
    log = _install_mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"hello": "world"}))
    out = await _http.get_json(
        "https://api.example.com", "/v1/thing",
        headers={"x-custom": "v"}, params={"q": "x", "n": 5},
    )
    assert out == {"hello": "world"}

    (req,) = log["requests"]
    assert req.method == "GET"
    assert req.url.path == "/v1/thing"
    assert req.url.params.get("q") == "x"
    assert req.url.params.get("n") == "5"  # non-str params are stringified by httpx
    assert req.headers["x-custom"] == "v"

    ctor = log["ctor"][0]
    assert ctor["timeout"] == 20.0  # get_json pins a 20s timeout
    assert ctor["follow_redirects"] is True


async def test_post_json_sends_body_and_default_timeout(monkeypatch):
    log = _install_mock_httpx(monkeypatch, lambda r: httpx.Response(201, json={"created": True}))
    out = await _http.post_json("https://x.io", "/p", json={"a": 1, "b": [2, 3]})
    assert out == {"created": True}

    (req,) = log["requests"]
    assert req.method == "POST"
    assert _json.loads(req.content) == {"a": 1, "b": [2, 3]}
    assert log["ctor"][0]["timeout"] == 60.0  # post default


async def test_post_json_custom_timeout_is_forwarded(monkeypatch):
    log = _install_mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={}))
    await _http.post_json("https://x.io", "/p", json={}, timeout=12.5)
    assert log["ctor"][0]["timeout"] == 12.5


async def test_base_trailing_slash_is_normalized(monkeypatch):
    seen = []

    def responder(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    _install_mock_httpx(monkeypatch, responder)
    await _http.get_json("https://h.com/", "/p")       # trailing slash on base
    await _http.get_json("https://h.com", "/p")         # no trailing slash
    assert seen == ["https://h.com/p", "https://h.com/p"]


async def test_headers_default_to_empty_when_none(monkeypatch):
    log = _install_mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))
    out = await _http.get_json("https://h.com", "/p")  # headers=None
    assert out == {"ok": 1}
    assert "x-custom" not in log["requests"][0].headers


async def test_non_json_response_falls_back_to_truncated_text(monkeypatch):
    _install_mock_httpx(monkeypatch, lambda r: httpx.Response(200, text="y" * 600))
    out = await _http.get_json("https://h.com", "/p")
    assert out["status_code"] == 200
    assert out["text"] == "y" * 500  # body truncated to 500 chars


async def test_retryable_status_then_success(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    _install_mock_httpx(monkeypatch, responder)
    out = await _http.get_json("https://h.com", "/p")
    assert out == {"ok": True}
    assert calls["n"] == 2
    assert sleeps == [1]  # one backoff before the successful retry


async def test_persistent_retryable_status_raises_after_max_attempts(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        return httpx.Response(503)

    _install_mock_httpx(monkeypatch, responder)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await _http.get_json("https://h.com", "/p")
    assert exc.value.response.status_code == 503
    assert calls["n"] == 3  # _MAX_ATTEMPTS
    assert sleeps == [1, 2]  # backoff before attempts 2 and 3


async def test_non_retryable_status_raises_immediately(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        return httpx.Response(404)

    _install_mock_httpx(monkeypatch, responder)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await _http.get_json("https://h.com", "/p")
    assert exc.value.response.status_code == 404
    assert calls["n"] == 1  # 4xx (non-retryable) -> no retry
    assert sleeps == []


async def test_transport_error_retries_then_succeeds(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"recovered": True})

    _install_mock_httpx(monkeypatch, responder)
    out = await _http.post_json("https://h.com", "/p", json={"a": 1})
    assert out == {"recovered": True}
    assert calls["n"] == 3
    assert sleeps == [1, 2]


async def test_persistent_transport_error_raises_after_max_attempts(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        raise httpx.ConnectError("down", request=request)

    _install_mock_httpx(monkeypatch, responder)
    with pytest.raises(httpx.ConnectError):
        await _http.get_json("https://h.com", "/p")
    assert calls["n"] == 3
    assert sleeps == [1, 2]


async def test_timeout_is_treated_as_retryable_transport_error(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow", request=request)

    _install_mock_httpx(monkeypatch, responder)
    with pytest.raises(httpx.TimeoutException):
        await _http.get_json("https://h.com", "/p")
    assert calls["n"] == 3
    assert sleeps == [1, 2]


async def test_backoff_is_capped_at_eight_seconds(monkeypatch):
    # With more attempts allowed, 2**attempt would exceed the cap: min(2**attempt, 8)
    # holds the delay at 8s (attempt 4 -> 16 capped to 8).
    monkeypatch.setattr(_http, "_MAX_ATTEMPTS", 6)
    sleeps = _record_sleeps(monkeypatch)
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        return httpx.Response(500)

    _install_mock_httpx(monkeypatch, responder)
    with pytest.raises(httpx.HTTPStatusError):
        await _http.get_json("https://h.com", "/p")
    assert calls["n"] == 6
    assert sleeps == [1, 2, 4, 8, 8]  # last delay would be 16 without the cap


if __name__ == "__main__":
    import asyncio
    import inspect

    for _name, _fn in sorted(globals().items()):
        if not (_name.startswith("test_") and callable(_fn)):
            continue
        _kwargs = {}
        _mp = None
        if "monkeypatch" in inspect.signature(_fn).parameters:
            _mp = pytest.MonkeyPatch()
            _kwargs["monkeypatch"] = _mp
        try:
            _res = _fn(**_kwargs)
            if inspect.iscoroutine(_res):
                asyncio.run(_res)
        finally:
            if _mp is not None:
                _mp.undo()
        print(f"PASS {_name}")
