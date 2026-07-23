"""HTTP + enforcement tests for app-configurable spend caps
(GET/POST /api/spend-caps, /reset) and that the Governor honors the override.
Dev-logged-in ASGI client against the live DB; the spend_caps app_setting row is
scrubbed after each test."""

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
async def _scrub_caps():
    yield
    try:
        from sqlalchemy import delete
        from switchboard.context import RunContext
        from switchboard.db.models import AppSetting
        from switchboard.governor.caps_config import SPEND_CAPS_KEY
        async with RunContext.open() as c:
            await c.session.execute(delete(AppSetting).where(AppSetting.key == SPEND_CAPS_KEY))
            await c.session.commit()
    except Exception:  # noqa: BLE001
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
    assert (await anon.get("/api/spend-caps")).status_code == 401
    assert (await anon.post("/api/spend-caps", json={})).status_code == 401
    assert (await anon.post("/api/spend-caps/reset", json={})).status_code == 401


async def test_get_defaults_when_no_override(api):
    r = await api.get("/api/spend-caps")
    assert r.status_code == 200
    body = r.json()
    assert set(body["caps"]) == {"enabled", "llm_usd_per_day", "bq_gib_per_day",
                                 "ahrefs_units_per_day"}
    assert body["customized"] is False
    assert body["defaults"]["llm_usd_per_day"] == 20.0  # shipped default


async def test_save_then_read_roundtrip(api):
    body = {"enabled": True, "llm_usd_per_day": 12.5, "bq_gib_per_day": 40,
            "ahrefs_units_per_day": 250}
    r = await api.post("/api/spend-caps", json=body)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["caps"] == {"enabled": True, "llm_usd_per_day": 12.5,
                                "bq_gib_per_day": 40.0, "ahrefs_units_per_day": 250}
    got = (await api.get("/api/spend-caps")).json()
    assert got["customized"] is True
    assert got["caps"]["llm_usd_per_day"] == 12.5


async def test_save_rejects_bad_value(api):
    r = await api.post("/api/spend-caps",
                       json={"enabled": True, "llm_usd_per_day": "lots",
                             "bq_gib_per_day": 40, "ahrefs_units_per_day": 250})
    assert r.status_code == 400


async def test_reset_reverts_to_defaults(api):
    await api.post("/api/spend-caps", json={"enabled": False, "llm_usd_per_day": 1,
                                            "bq_gib_per_day": 1, "ahrefs_units_per_day": 1})
    assert (await api.get("/api/spend-caps")).json()["customized"] is True
    r = await api.post("/api/spend-caps/reset", json={})
    assert r.status_code == 200 and r.json()["reset"] is True
    assert (await api.get("/api/spend-caps")).json()["customized"] is False


async def test_governor_page_reflects_disabled_override(api):
    # Disabling via the override must show up as caps_enabled=false on /api/governor.
    await api.post("/api/spend-caps", json={"enabled": False, "llm_usd_per_day": 20,
                                            "bq_gib_per_day": 100, "ahrefs_units_per_day": 5000})
    gov = (await api.get("/api/governor")).json()
    assert gov["caps_enabled"] is False


async def test_governor_enforcement_honors_override(api):
    # The Governor's budget check must resolve the override, not the env default.
    from switchboard.context import RunContext
    from switchboard.governor.caps_config import save_caps
    from switchboard.governor.governor import Governor

    async with RunContext.open() as ctx:
        # $1/day LLM cap, enforced: 2M micros ($2) additional must exceed it.
        await save_caps(ctx.session, enabled=True, llm_usd_per_day=1, bq_gib_per_day=100,
                        ahrefs_units_per_day=5000)
        await ctx.session.commit()
        assert await Governor(ctx.session).within_caps("llm_micros", additional=2_000_000) is False

    async with RunContext.open() as ctx:
        # Disabled -> no cap -> always within.
        await save_caps(ctx.session, enabled=False, llm_usd_per_day=1, bq_gib_per_day=100,
                        ahrefs_units_per_day=5000)
        await ctx.session.commit()
        assert await Governor(ctx.session).within_caps("llm_micros", additional=2_000_000) is True
