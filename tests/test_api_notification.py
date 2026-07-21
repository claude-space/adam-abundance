"""HTTP tests for /api/notification-config (the Integrations admin config).
Dev-logged-in ASGI client (global_admin) against the live DB; the singleton
app_setting[trend_alert] row is cleaned up after each test."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

DEV_EMAIL = "andrew.marks@valnetinc.com"


async def _require_db():
    from switchboard.context import RunContext
    try:
        async with RunContext.open() as c:
            await c.store.expire_stale()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no reachable Postgres: {exc}")


@pytest.fixture(autouse=True)
async def _scrub_setting():
    """Delete the trend_alert app_setting row after each test (shared DB)."""
    yield
    try:
        from sqlalchemy import delete
        from switchboard.context import RunContext
        from switchboard.db.models import AppSetting
        from switchboard.notifications import TREND_ALERT_KEY
        async with RunContext.open() as c:
            await c.session.execute(delete(AppSetting).where(AppSetting.key == TREND_ALERT_KEY))
            await c.session.commit()
    except Exception:  # noqa: BLE001 — no DB / nothing to scrub
        pass


@pytest.fixture
async def anon():
    from switchboard.api.app import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           follow_redirects=False) as ac:
        yield ac


@pytest.fixture
async def api():
    await _require_db()
    from switchboard.api.app import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           follow_redirects=True) as ac:
        r = await ac.post("/auth/dev-login", data={"email": DEV_EMAIL})
        assert r.status_code in (200, 302), r.text[:300]
        yield ac


async def test_get_defaults_when_unset(api):
    r = await api.get("/api/notification-config")
    assert r.status_code == 200
    ta = r.json()["trend_alert"]
    assert set(ta) >= {"enabled", "webhook_url", "min_score"}
    assert ta["enabled"] is False  # default


async def test_requires_auth(anon):
    assert (await anon.get("/api/notification-config")).status_code == 401
    assert (await anon.post("/api/notification-config", json={})).status_code == 401
    assert (await anon.post("/api/notification-config/test", json={})).status_code == 401


async def test_save_then_read_roundtrip(api):
    body = {"trend_alert": {"enabled": True, "webhook_url": "https://example.test/hook",
                            "min_score": 82}}
    r = await api.post("/api/notification-config", json=body)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["trend_alert"] == {"enabled": True, "webhook_url": "https://example.test/hook",
                                       "min_score": 82.0}
    got = (await api.get("/api/notification-config")).json()["trend_alert"]
    assert got["enabled"] is True and got["webhook_url"] == "https://example.test/hook"
    assert got["min_score"] == 82.0


async def test_save_clamps_and_normalizes(api):
    r = await api.post("/api/notification-config",
                       json={"trend_alert": {"enabled": 1, "webhook_url": "https://h", "min_score": 500}})
    assert r.status_code == 200 and r.json()["trend_alert"]["min_score"] == 100.0


async def test_save_rejects_bad_url(api):
    r = await api.post("/api/notification-config",
                       json={"trend_alert": {"enabled": True, "webhook_url": "ftp://nope", "min_score": 70}})
    assert r.status_code == 400


async def test_test_ping_without_url_is_400(api):
    # nothing configured yet → no URL to ping
    assert (await api.post("/api/notification-config/test", json={})).status_code == 400


async def test_test_ping_uses_body_url(api, monkeypatch):
    # Avoid a real network call: stub the low-level POST.
    from switchboard import notifications as N

    async def _fake_post(url, payload):
        return True

    monkeypatch.setattr(N, "_post", _fake_post)
    r = await api.post("/api/notification-config/test",
                       json={"webhook_url": "https://example.test/hook"})
    assert r.status_code == 200 and r.json()["ok"] is True
