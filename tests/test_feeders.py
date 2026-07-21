"""Unit tests for the scheduled feeders (PRD §6 "Scheduled feeders (NOT agents)").

Covers the three feeders + the package factory/entrypoint:

* ``decay.DecayScanFeeder``          — Seona decay candidates → FLAG entries
* ``content_audit.ContentAuditFeeder`` — content-depth findings → FLAG entries
* ``trend_scan.TrendScanFeeder``     — thin wrapper over the (lazily-imported) TrendScout
* ``feeders.build_feeder`` / ``run_feeder``

Everything is self-contained: the shared-memory ``store`` is a plain recorder (no
DB), the HTTP-JSON helper (``get_json``) is monkeypatched at the module boundary
(no network), the lazily-imported ``TrendScout`` is swapped for a fake, and
``RunContext.open`` is replaced with an async CM yielding a fake ctx.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import switchboard.feeders as feeders
import switchboard.feeders.content_audit as ca_mod
import switchboard.feeders.decay as decay_mod
import switchboard.trends.scout as scout_mod
from switchboard.adapters.base import AdapterUnavailable
from switchboard.db.enums import EntryType
from switchboard.feeders import build_feeder, run_feeder
from switchboard.feeders.content_audit import ContentAuditFeeder, _rel_drop
from switchboard.feeders.decay import DecayScanFeeder
from switchboard.feeders.trend_scan import TrendScanFeeder


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeStore:
    """Records every ``write(draft)`` instead of persisting a row."""

    def __init__(self) -> None:
        self.writes: list = []

    async def write(self, draft, **kwargs):
        self.writes.append(draft)
        return draft


class FakeCreds:
    def __init__(self, values: dict | None = None) -> None:
        self._d = values or {}

    def resolve(self, key, *, required: bool = False, secret: bool = True):
        return self._d.get(key)


def _decay_ctx(*, seona: str | None = "http://seona.local", creds: FakeCreds | None = None):
    endpoints = {"seona": seona} if seona is not None else {}
    return SimpleNamespace(
        settings=SimpleNamespace(endpoints=endpoints),
        creds=creds or FakeCreds(),
        store=FakeStore(),
    )


def _ca_ctx(creds_values: dict | None = None):
    return SimpleNamespace(creds=FakeCreds(creds_values or {}), store=FakeStore())


def _patch_get_json(monkeypatch, mod, *, result=None, exc=None):
    """Swap ``mod.get_json`` for a recorder. Returns the captured-call list."""
    calls: list = []

    async def _gj(base, path, headers=None, params=None):
        calls.append(SimpleNamespace(base=base, path=path, headers=headers, params=params))
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(mod, "get_json", _gj)
    return calls


# ---------------------------------------------------------------------------
# feeders/__init__ — registry, build_feeder, run_feeder
# ---------------------------------------------------------------------------


def test_feeder_registry_and_names():
    assert set(feeders._FEEDERS) == {"decay", "content_audit", "trend_scan"}
    assert DecayScanFeeder.name == "decay_scan"
    assert ContentAuditFeeder.name == "content_audit"
    assert TrendScanFeeder.name == "trend_scan"
    assert set(feeders.__all__) >= {
        "DecayScanFeeder", "ContentAuditFeeder", "TrendScanFeeder", "build_feeder", "run_feeder",
    }


def test_build_feeder_known_returns_instance_bound_to_ctx():
    ctx = object()
    f = build_feeder("decay", ctx)
    assert isinstance(f, DecayScanFeeder)
    assert f.ctx is ctx
    assert isinstance(build_feeder("content_audit", ctx), ContentAuditFeeder)
    assert isinstance(build_feeder("trend_scan", ctx), TrendScanFeeder)


def test_build_feeder_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        build_feeder("does-not-exist", object())


async def test_run_feeder_opens_context_builds_and_runs(monkeypatch):
    seen = {}

    class FakeFeeder:
        def __init__(self, ctx):
            seen["ctx"] = ctx

        async def run(self, brand):
            seen["brand"] = brand
            return 11

    fake_ctx = object()

    @asynccontextmanager
    async def fake_open(*a, **k):
        yield fake_ctx

    monkeypatch.setattr(feeders.RunContext, "open", fake_open)
    monkeypatch.setitem(feeders._FEEDERS, "fake", FakeFeeder)

    out = await run_feeder("fake", "hotcars")
    assert out == 11
    assert seen["ctx"] is fake_ctx
    assert seen["brand"] == "hotcars"


# ---------------------------------------------------------------------------
# trend_scan feeder
# ---------------------------------------------------------------------------


async def test_trend_scan_sums_new_and_updated(monkeypatch):
    class FakeScout:
        def __init__(self, ctx):
            self.ctx = ctx

        async def scan(self, brand):
            assert brand == "portfolio"
            return {"enabled": True, "new_trends": 3, "updated_trends": 2}

    monkeypatch.setattr(scout_mod, "TrendScout", FakeScout)
    assert await TrendScanFeeder(object()).run("portfolio") == 5


async def test_trend_scan_missing_keys_default_to_zero(monkeypatch):
    class FakeScout:
        def __init__(self, ctx):
            pass

        async def scan(self, brand):
            return {"enabled": False}  # disabled scan -> no counters

    monkeypatch.setattr(scout_mod, "TrendScout", FakeScout)
    assert await TrendScanFeeder(object()).run("portfolio") == 0


async def test_trend_scan_only_new_trends(monkeypatch):
    class FakeScout:
        def __init__(self, ctx):
            pass

        async def scan(self, brand):
            return {"new_trends": 4}

    monkeypatch.setattr(scout_mod, "TrendScout", FakeScout)
    assert await TrendScanFeeder(object()).run("hotcars") == 4


async def test_trend_scan_scout_exception_degrades_to_zero(monkeypatch):
    class FakeScout:
        def __init__(self, ctx):
            pass

        async def scan(self, brand):
            raise RuntimeError("scout exploded")

    monkeypatch.setattr(scout_mod, "TrendScout", FakeScout)
    assert await TrendScanFeeder(object()).run("portfolio") == 0


# ---------------------------------------------------------------------------
# decay feeder
# ---------------------------------------------------------------------------


async def test_decay_populated_writes_flags_with_expected_shape(monkeypatch):
    ctx = _decay_ctx()
    calls = _patch_get_json(monkeypatch, decay_mod, result=[
        {"url": "https://a", "pos_delta": 2.5, "click_ratio": 0.6},
        {"url": "https://b", "pos_delta": 3.0, "click_ratio": 0.5},
    ])
    n = await DecayScanFeeder(ctx).run("hotcars")
    assert n == 2
    assert len(ctx.store.writes) == 2

    d0 = ctx.store.writes[0]
    assert d0.type == EntryType.FLAG
    assert d0.brand == "hotcars"
    assert d0.source_agent == "decay_scan"
    assert d0.source_system == "seona"
    assert d0.payload == {
        "kind": "decay_candidate", "url": "https://a",
        "pos_delta": 2.5, "click_ratio": 0.6, "severity": "medium",
    }
    assert d0.source_urls == ["https://a"]
    assert d0.ttl_seconds == 3 * 24 * 3600

    # Endpoint + params: default path (creds miss), seona base, brand param.
    assert calls[0].base == "http://seona.local"
    assert calls[0].path == "/api/decay/candidates"
    assert calls[0].params == {"brand": "hotcars"}


async def test_decay_empty_list_writes_nothing(monkeypatch):
    ctx = _decay_ctx()
    _patch_get_json(monkeypatch, decay_mod, result=[])
    assert await DecayScanFeeder(ctx).run("hotcars") == 0
    assert ctx.store.writes == []


async def test_decay_dict_candidates_key(monkeypatch):
    ctx = _decay_ctx()
    _patch_get_json(monkeypatch, decay_mod, result={"candidates": [{"url": "u"}]})
    assert await DecayScanFeeder(ctx).run("hotcars") == 1


async def test_decay_dict_data_key_fallback(monkeypatch):
    ctx = _decay_ctx()
    _patch_get_json(monkeypatch, decay_mod, result={"data": [{"url": "u1"}, {"url": "u2"}]})
    assert await DecayScanFeeder(ctx).run("hotcars") == 2


async def test_decay_dict_with_neither_key_writes_nothing(monkeypatch):
    ctx = _decay_ctx()
    _patch_get_json(monkeypatch, decay_mod, result={"unrelated": 1})
    assert await DecayScanFeeder(ctx).run("hotcars") == 0


async def test_decay_permalink_fallback_and_no_source_urls(monkeypatch):
    ctx = _decay_ctx()
    _patch_get_json(monkeypatch, decay_mod, result=[{"permalink": "https://perma", "pos_delta": 1}])
    assert await DecayScanFeeder(ctx).run("hotcars") == 1
    d = ctx.store.writes[0]
    assert d.payload["url"] == "https://perma"   # url falls back to permalink
    assert d.source_urls is None                 # but source_urls only set from 'url'


async def test_decay_custom_path_from_creds(monkeypatch):
    ctx = _decay_ctx(creds=FakeCreds({"SEONA_DECAY_LIST_PATH": "/custom/list"}))
    calls = _patch_get_json(monkeypatch, decay_mod, result=[])
    await DecayScanFeeder(ctx).run("hotcars")
    assert calls[0].path == "/custom/list"


async def test_decay_adapter_unavailable_returns_zero(monkeypatch):
    ctx = _decay_ctx()
    _patch_get_json(monkeypatch, decay_mod, exc=AdapterUnavailable("httpx missing"))
    assert await DecayScanFeeder(ctx).run("hotcars") == 0
    assert ctx.store.writes == []


async def test_decay_generic_error_returns_zero(monkeypatch):
    ctx = _decay_ctx()
    _patch_get_json(monkeypatch, decay_mod, exc=RuntimeError("seona 500"))
    assert await DecayScanFeeder(ctx).run("hotcars") == 0


async def test_decay_caps_at_fifty(monkeypatch):
    ctx = _decay_ctx()
    _patch_get_json(monkeypatch, decay_mod, result=[{"url": f"u{i}"} for i in range(60)])
    assert await DecayScanFeeder(ctx).run("hotcars") == 50
    assert len(ctx.store.writes) == 50


async def test_decay_null_body_currently_raises(monkeypatch):
    # ACTUAL behavior: a JSON `null` body -> get_json returns None -> the
    # `data.get(...)` line (OUTSIDE the try/except) raises AttributeError. Documents
    # a latent robustness gap (non-list/non-dict bodies are not defended).
    ctx = _decay_ctx()
    _patch_get_json(monkeypatch, decay_mod, result=None)
    with pytest.raises(AttributeError):
        await DecayScanFeeder(ctx).run("hotcars")


# ---------------------------------------------------------------------------
# content_audit — _rel_drop helper
# ---------------------------------------------------------------------------


def test_rel_drop_material_decline():
    assert _rel_drop(100, 75) == 0.25
    assert _rel_drop(100, 50) == 0.5


def test_rel_drop_none_or_nonpositive_baseline():
    assert _rel_drop(None, 5) == 0.0
    assert _rel_drop(5, None) == 0.0
    assert _rel_drop(0, 5) == 0.0
    assert _rel_drop(-3, 5) == 0.0


def test_rel_drop_held_or_improved_clamps_to_zero():
    assert _rel_drop(100, 100) == 0.0   # held
    assert _rel_drop(100, 130) == 0.0   # improved -> max(0, negative)


def test_rel_drop_non_numeric_is_swallowed():
    assert _rel_drop("abc", 5) == 0.0        # ValueError on float("abc")
    assert _rel_drop(object(), 5) == 0.0     # TypeError on float(object())


# ---------------------------------------------------------------------------
# content_audit feeder
# ---------------------------------------------------------------------------


async def test_content_audit_flags_only_material_brand_matched_drops(monkeypatch):
    ctx = _ca_ctx()
    records = [
        # depth drop 30% -> high
        {"url": "u1", "property_id": "www.hotcars.com", "status": "live",
         "baseline_depth_pct": 100, "snapshot_depth_pct": 70,
         "baseline_avd_seconds": 50, "snapshot_avd_seconds": 50},
        # avd drop 15% (depth held) -> medium
        {"url": "u2", "property_id": "www.hotcars.com",
         "baseline_depth_pct": 100, "snapshot_depth_pct": 100,
         "baseline_avd_seconds": 100, "snapshot_avd_seconds": 85},
        # worst 5% -> below 0.10 threshold, skipped
        {"url": "u3", "property_id": "www.hotcars.com",
         "baseline_depth_pct": 100, "snapshot_depth_pct": 95,
         "baseline_avd_seconds": 100, "snapshot_avd_seconds": 95},
        # no url -> skipped before brand match
        {"property_id": "www.hotcars.com", "baseline_depth_pct": 100, "snapshot_depth_pct": 10},
        # other brand's property -> filtered out
        {"url": "u5", "property_id": "www.carbuzz.com",
         "baseline_depth_pct": 100, "snapshot_depth_pct": 10},
        # empty property_id -> brand filter does NOT apply -> matched, high
        {"url": "u6", "property_id": "",
         "baseline_depth_pct": 100, "snapshot_depth_pct": 50},
    ]
    calls = _patch_get_json(monkeypatch, ca_mod, result={"records": records})
    n = await ContentAuditFeeder(ctx).run("hotcars")

    assert n == 3
    assert [w.payload["severity"] for w in ctx.store.writes] == ["high", "medium", "high"]

    w0 = ctx.store.writes[0]
    assert w0.type == EntryType.FLAG
    assert w0.source_agent == "content_audit"
    assert w0.source_system == "content_depth_auditor"
    assert w0.payload["kind"] == "content_audit_finding"
    assert w0.payload["url"] == "u1"
    assert w0.payload["property_id"] == "www.hotcars.com"
    assert w0.payload["status"] == "live"
    assert w0.payload["depth_pct"] == 70
    assert w0.payload["baseline_depth_pct"] == 100
    assert w0.payload["depth_drop_pct"] == 30.0
    assert w0.payload["avd_drop_pct"] == 0.0
    assert w0.source_urls == ["u1"]
    assert w0.ttl_seconds == 5 * 24 * 3600

    # The medium finding is driven purely by the AVD drop.
    w1 = ctx.store.writes[1]
    assert w1.payload["avd_drop_pct"] == 15.0
    assert w1.payload["depth_drop_pct"] == 0.0

    # Defaults + headers: no token -> no Authorization, but the UA is always set.
    assert calls[0].base == "http://localhost:8600"
    assert calls[0].path == "/api/tracking"
    assert calls[0].params == {"brand": "hotcars"}
    assert "Authorization" not in calls[0].headers
    assert calls[0].headers["User-Agent"].startswith("Switchboard/")


async def test_content_audit_severity_boundaries(monkeypatch):
    ctx = _ca_ctx()
    records = [
        {"url": "hi", "property_id": "www.hotcars.com",
         "baseline_depth_pct": 100, "snapshot_depth_pct": 75},   # exactly 0.25 -> high
        {"url": "md", "property_id": "www.hotcars.com",
         "baseline_depth_pct": 100, "snapshot_depth_pct": 90},   # exactly 0.10 -> medium
    ]
    _patch_get_json(monkeypatch, ca_mod, result=records)  # bare-list shape
    assert await ContentAuditFeeder(ctx).run("hotcars") == 2
    assert ctx.store.writes[0].payload["severity"] == "high"
    assert ctx.store.writes[1].payload["severity"] == "medium"


async def test_content_audit_adds_auth_header_when_token_present(monkeypatch):
    ctx = _ca_ctx({"CONTENT_AUDITOR_TOKEN": "SEKRET"})
    calls = _patch_get_json(monkeypatch, ca_mod, result={"records": []})
    await ContentAuditFeeder(ctx).run("hotcars")
    assert calls[0].headers["Authorization"] == "Bearer SEKRET"
    assert calls[0].headers["User-Agent"].startswith("Switchboard/")


async def test_content_audit_custom_url_and_path(monkeypatch):
    ctx = _ca_ctx({"CONTENT_AUDITOR_URL": "http://audit:9000",
                   "CONTENT_AUDITOR_TRACKING_PATH": "/t"})
    calls = _patch_get_json(monkeypatch, ca_mod, result=[])
    await ContentAuditFeeder(ctx).run("hotcars")
    assert calls[0].base == "http://audit:9000"
    assert calls[0].path == "/t"


async def test_content_audit_tracking_and_items_shapes(monkeypatch):
    rec = {"url": "u", "property_id": "www.hotcars.com",
           "baseline_depth_pct": 100, "snapshot_depth_pct": 10}
    ctx = _ca_ctx()
    _patch_get_json(monkeypatch, ca_mod, result={"tracking": [rec]})
    assert await ContentAuditFeeder(ctx).run("hotcars") == 1

    ctx2 = _ca_ctx()
    _patch_get_json(monkeypatch, ca_mod, result={"items": [rec]})
    assert await ContentAuditFeeder(ctx2).run("hotcars") == 1


async def test_content_audit_empty_returns_zero(monkeypatch):
    ctx = _ca_ctx()
    _patch_get_json(monkeypatch, ca_mod, result={"records": []})
    assert await ContentAuditFeeder(ctx).run("hotcars") == 0
    assert ctx.store.writes == []


async def test_content_audit_adapter_unavailable_returns_zero(monkeypatch):
    ctx = _ca_ctx()
    _patch_get_json(monkeypatch, ca_mod, exc=AdapterUnavailable("httpx missing"))
    assert await ContentAuditFeeder(ctx).run("hotcars") == 0


async def test_content_audit_generic_error_returns_zero(monkeypatch):
    ctx = _ca_ctx()
    _patch_get_json(monkeypatch, ca_mod, exc=RuntimeError("403 forbidden"))
    assert await ContentAuditFeeder(ctx).run("hotcars") == 0


async def test_content_audit_caps_at_fifty(monkeypatch):
    ctx = _ca_ctx()
    recs = [{"url": f"u{i}", "property_id": "www.hotcars.com",
             "baseline_depth_pct": 100, "snapshot_depth_pct": 10} for i in range(60)]
    _patch_get_json(monkeypatch, ca_mod, result=recs)
    assert await ContentAuditFeeder(ctx).run("hotcars") == 50
    assert len(ctx.store.writes) == 50
