"""HTTP-level tests for the JSON API (``/api/*``) via an in-process ASGI client.

Uses httpx AsyncClient + ASGITransport (not starlette TestClient) so every
request runs on the same event loop as the test — the module-cached asyncpg
engine is created and disposed on one loop, avoiding cross-loop pool errors.

DB-backed: the whole module skips when no Postgres is reachable. CI provides a
Postgres service (schema migrated with ``alembic upgrade head``); locally the
docker DB on :5544 is used via DATABASE_URL.
"""

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


@pytest.fixture
async def anon():
    """An unauthenticated ASGI client."""
    from switchboard.api.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as ac:
        yield ac


@pytest.fixture
async def api():
    """A dev-logged-in ASGI client (skips if no DB)."""
    await _require_db()
    from switchboard.api.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as ac:
        r = await ac.post("/auth/dev-login", data={"email": DEV_EMAIL})
        assert r.status_code in (200, 302), r.text[:300]
        yield ac


# --- auth gate -------------------------------------------------------------

async def test_me_requires_auth(anon):
    r = await anon.get("/api/me")
    assert r.status_code == 401


async def test_me_after_login(api):
    r = await api.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == DEV_EMAIL
    assert "role" in body and "brands" in body


async def test_unknown_api_path_404(api):
    assert (await api.get("/api/does-not-exist")).status_code == 404


# --- read endpoints return 200 + a JSON body on the live DB ----------------

READ_ENDPOINTS = [
    "/api/dashboard",
    "/api/agents",
    "/api/systems",
    "/api/activity",
    "/api/notifications",
    "/api/trends",
    "/api/pipelines",
    "/api/distribution",
    "/api/observability",
    "/api/governor",
    "/api/session-trends",
    "/api/expenditure",
    "/api/plans",
    "/api/writers",
    "/api/personas",
    "/api/trend-score-weights",
    "/api/users",
    "/api/approvals",
    "/api/memory/ledger",
]


@pytest.mark.parametrize("path", READ_ENDPOINTS)
async def test_read_endpoint_ok(api, path):
    r = await api.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert isinstance(body, (dict, list))


@pytest.mark.parametrize("path", READ_ENDPOINTS)
async def test_read_endpoint_requires_auth(anon, path):
    assert (await anon.get(path)).status_code == 401


# --- the logo cache-proxy --------------------------------------------------

async def test_logo_rejects_bad_domain(api):
    assert (await api.get("/api/logo", params={"d": "../etc/passwd"})).status_code == 400


async def test_logo_no_token_configured_returns_404(api):
    # Local/CI test env has no LOGO_DEV_TOKEN, so a well-formed domain that isn't
    # already cached resolves to 404 "logo source not configured" (never a 500).
    r = await api.get("/api/logo", params={"d": "example.com", "s": 64})
    assert r.status_code in (404, 200)


async def test_logo_requires_auth(anon):
    assert (await anon.get("/api/logo", params={"d": "ford.com"})).status_code == 401


# --- not-found detail routes ----------------------------------------------

async def test_unknown_trend_404(api):
    assert (await api.get("/api/trends/99999999")).status_code == 404


async def test_unknown_pipeline_404(api):
    assert (await api.get("/api/pipelines/99999999")).status_code == 404


async def test_unknown_plan_404(api):
    r = await api.get("/api/plans/99999999")
    assert r.status_code == 404
