"""HTTP tests for per-user notification read-state
(GET /api/notifications + POST /read + /read-all). Dev-logged-in ASGI client
against the live DB; the user's notification_read rows are scrubbed after each
test so runs are independent."""

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
async def _scrub_reads():
    """Drop this user's read-state after each test (shared DB)."""
    yield
    try:
        from sqlalchemy import delete
        from switchboard.context import RunContext
        from switchboard.db.models import NotificationRead
        async with RunContext.open() as c:
            await c.session.execute(
                delete(NotificationRead).where(NotificationRead.user_email == DEV_EMAIL))
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


async def test_requires_auth(anon):
    assert (await anon.get("/api/notifications")).status_code == 401
    assert (await anon.post("/api/notifications/read", json={"keys": []})).status_code == 401
    assert (await anon.post("/api/notifications/read-all", json={})).status_code == 401


async def test_items_carry_key_and_read(api):
    items = (await api.get("/api/notifications")).json()["items"]
    assert isinstance(items, list)
    for n in items:
        assert isinstance(n.get("key"), str) and n["key"]
        assert isinstance(n.get("read"), bool)


async def test_mark_specific_keys_is_idempotent(api):
    keys = ["flag:99990001", "plan_item:99990002"]
    r1 = await api.post("/api/notifications/read", json={"keys": keys})
    assert r1.status_code == 200 and r1.json() == {"ok": True, "read": 2}
    # marking the same keys again adds nothing (persisted + idempotent)
    r2 = await api.post("/api/notifications/read", json={"keys": keys})
    assert r2.json()["read"] == 0
    # empty / missing keys are a no-op, not an error
    assert (await api.post("/api/notifications/read", json={"keys": []})).json()["read"] == 0
    assert (await api.post("/api/notifications/read", json={})).json()["read"] == 0


async def test_read_all_marks_everything_and_persists(api):
    # Baseline: however many actionable items exist right now.
    before = (await api.get("/api/notifications")).json()["items"]

    res = await api.post("/api/notifications/read-all", json={})
    assert res.status_code == 200 and res.json()["ok"] is True
    assert res.json()["read"] == sum(1 for n in before if not n["read"])

    # A FRESH GET (new RunContext / new query) must now show every item read —
    # this is the persistence the old client-only state lacked.
    after = (await api.get("/api/notifications")).json()["items"]
    assert all(n["read"] for n in after)

    # Marking all again is a no-op.
    assert (await api.post("/api/notifications/read-all", json={})).json()["read"] == 0


async def test_get_degrades_when_read_state_unavailable(api, monkeypatch):
    """If the read-state store errors (e.g. migration 0012 not yet applied on a
    fresh deploy), the feed must still render — all items simply read as unread."""
    from switchboard.api import routes

    async def _boom(*_a, **_k):
        raise RuntimeError("relation \"notification_read\" does not exist")

    monkeypatch.setattr(routes, "_read_notification_keys", _boom)
    r = await api.get("/api/notifications")
    assert r.status_code == 200
    items = r.json()["items"]
    assert isinstance(items, list)
    assert all(n["read"] is False for n in items)
