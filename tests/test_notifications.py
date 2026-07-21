"""Unit tests for the trend-alert webhook (notifications.py). No DB, no network:
a duck-typed fake async session for load/save, httpx mocked at the boundary."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from switchboard import notifications as N


# --- fakes ----------------------------------------------------------------

class _Result:
    def __init__(self, scalar: Any = None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _Session:
    """Returns queued results from execute(); records add()/flush()."""

    def __init__(self, results: list[_Result]):
        self._results = list(results)
        self.added: list[Any] = []
        self.flushed = 0

    async def execute(self, _stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1


def _trend(**over):
    base = dict(id=42, brand="hotcars", headline="Tesla recall widens", score=88.0,
                status="proposed", source_count=6, signal_count=11,
                entities={"oems": ["Tesla"], "models": ["Model Y"]})
    base.update(over)
    return SimpleNamespace(**base)


def _ctx(session, base_url=""):
    return SimpleNamespace(session=session,
                           creds=SimpleNamespace(resolve=lambda *a, **k: base_url))


class _FakeResp:
    def __init__(self, status_code=200):
        self.status_code = status_code


def _install_httpx(monkeypatch, *, status=200, boom=False):
    calls: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls.append({"url": url, "json": json})
            if boom:
                raise httpx.ConnectError("no route")
            return _FakeResp(status)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return calls


# --- _coerce --------------------------------------------------------------

def test_coerce_defaults_when_none():
    assert N._coerce(None) == N.DEFAULT_TREND_ALERT


def test_coerce_normalizes_types_and_clamps():
    cfg = N._coerce({"enabled": 1, "webhook_url": None, "min_score": 250})
    assert cfg["enabled"] is True
    assert cfg["webhook_url"] == ""
    assert cfg["min_score"] == 100.0  # clamped
    assert N._coerce({"min_score": "oops"})["min_score"] == N.DEFAULT_TREND_ALERT["min_score"]


# --- load / save ----------------------------------------------------------

async def test_load_trend_alert_from_row():
    s = _Session([_Result(scalar={"enabled": True, "webhook_url": "https://x", "min_score": 80})])
    cfg = await N.load_trend_alert(s)
    assert cfg == {"enabled": True, "webhook_url": "https://x", "min_score": 80.0}


async def test_load_trend_alert_defaults_when_unset():
    assert await N.load_trend_alert(_Session([_Result(scalar=None)])) == N.DEFAULT_TREND_ALERT


async def test_save_inserts_when_absent():
    s = _Session([_Result(scalar=None)])  # no existing row
    out = await N.save_trend_alert(s, enabled=True, webhook_url="https://hook", min_score=75)
    assert out == {"enabled": True, "webhook_url": "https://hook", "min_score": 75.0}
    assert len(s.added) == 1 and s.flushed == 1


async def test_save_updates_when_present():
    existing = SimpleNamespace(value={"enabled": False}, updated_by=None)
    s = _Session([_Result(scalar=existing)])
    await N.save_trend_alert(s, enabled=True, webhook_url="https://hook", min_score=90,
                             updated_by="a@b.com")
    assert s.added == []                       # updated in place, not added
    assert existing.value["enabled"] is True and existing.updated_by == "a@b.com"


async def test_save_rejects_non_http_url():
    with pytest.raises(ValueError):
        await N.save_trend_alert(_Session([]), enabled=True, webhook_url="ftp://x", min_score=70)


async def test_save_allows_empty_url():
    s = _Session([_Result(scalar=None)])
    out = await N.save_trend_alert(s, enabled=False, webhook_url="", min_score=70)
    assert out["webhook_url"] == ""


# --- payload --------------------------------------------------------------

def test_build_payload_shape():
    p = N._build_payload(_trend(), 70.0, "https://app.example", pipeline_id=9)
    assert p["event"] == "trend.sourced"
    assert p["trend"]["id"] == 42 and p["trend"]["brand"] == "hotcars"
    assert p["trend"]["score"] == 88.0 and p["trend"]["oems"] == ["Tesla"]
    assert p["trend"]["url"] == "https://app.example/trends/42"
    assert p["trend"]["pipeline_id"] == 9
    assert p["threshold"] == 70.0
    assert "Tesla recall widens" in p["text"] and p["text"].startswith(":fire:")


def test_build_payload_no_base_url_omits_link():
    p = N._build_payload(_trend(entities=None), 50.0, "", pipeline_id=None)
    assert p["trend"]["url"] is None and p["trend"]["oems"] == []
    assert "pipeline_id" not in p["trend"]
    assert "—" not in p["text"]


# --- fire_trend_alert -----------------------------------------------------

async def test_fire_posts_when_enabled_and_above_threshold(monkeypatch):
    calls = _install_httpx(monkeypatch)
    s = _Session([_Result(scalar={"enabled": True, "webhook_url": "https://hook", "min_score": 70})])
    ok = await N.fire_trend_alert(_ctx(s, "https://app"), _trend(score=88), pipeline_id=3)
    assert ok is True
    assert len(calls) == 1 and calls[0]["url"] == "https://hook"
    assert calls[0]["json"]["trend"]["pipeline_id"] == 3


async def test_fire_skips_when_disabled(monkeypatch):
    calls = _install_httpx(monkeypatch)
    s = _Session([_Result(scalar={"enabled": False, "webhook_url": "https://hook", "min_score": 70})])
    assert await N.fire_trend_alert(_ctx(s), _trend(score=99)) is False
    assert calls == []


async def test_fire_skips_when_no_url(monkeypatch):
    calls = _install_httpx(monkeypatch)
    s = _Session([_Result(scalar={"enabled": True, "webhook_url": "", "min_score": 70})])
    assert await N.fire_trend_alert(_ctx(s), _trend(score=99)) is False
    assert calls == []


async def test_fire_skips_below_threshold(monkeypatch):
    calls = _install_httpx(monkeypatch)
    s = _Session([_Result(scalar={"enabled": True, "webhook_url": "https://hook", "min_score": 90})])
    assert await N.fire_trend_alert(_ctx(s), _trend(score=80)) is False
    assert calls == []


async def test_fire_is_non_fatal_on_post_error(monkeypatch):
    _install_httpx(monkeypatch, boom=True)
    s = _Session([_Result(scalar={"enabled": True, "webhook_url": "https://hook", "min_score": 70})])
    assert await N.fire_trend_alert(_ctx(s, "https://app"), _trend(score=95)) is False


async def test_fire_non_2xx_is_false(monkeypatch):
    _install_httpx(monkeypatch, status=500)
    s = _Session([_Result(scalar={"enabled": True, "webhook_url": "https://hook", "min_score": 70})])
    assert await N.fire_trend_alert(_ctx(s, "https://app"), _trend(score=95)) is False


# --- test ping ------------------------------------------------------------

async def test_send_test_ping_ok(monkeypatch):
    calls = _install_httpx(monkeypatch)
    ok, detail = await N.send_test_ping("https://hook")
    assert ok is True and "accepted" in detail
    assert calls[0]["json"]["event"] == "trend.test"


async def test_send_test_ping_rejects_bad_url():
    ok, detail = await N.send_test_ping("not-a-url")
    assert ok is False and "http" in detail
