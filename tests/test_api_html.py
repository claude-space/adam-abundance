"""Smoke tests for the server-rendered (Jinja) routes + the small JSON helpers
in ``routes.py``. A dev-logged-in ASGI client GETs each page and asserts it
renders (200) on the live DB; unauth access is rejected. Same httpx AsyncClient
+ ASGITransport approach as test_api_json (single event loop per test).

DB-backed: the module skips when no Postgres is reachable.
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
    from switchboard.api.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as ac:
        yield ac


@pytest.fixture
async def web():
    await _require_db()
    from switchboard.api.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as ac:
        r = await ac.post("/auth/dev-login", data={"email": DEV_EMAIL})
        assert r.status_code in (200, 302), r.text[:300]
        yield ac


HTML_PAGES = [
    "/",
    "/overview",
    "/memory",
    "/agents",
    "/users",
    "/systems",
    "/session-trends",
    "/writer-emulation",
    "/expenditure",
    "/distribution",
    "/trends",
    "/pipelines",
    "/governor",
    "/observability",
    "/activity",
    "/notifications",
]


@pytest.mark.parametrize("path", HTML_PAGES)
async def test_html_page_renders(web, path):
    r = await web.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    assert "text/html" in r.headers.get("content-type", "")


@pytest.mark.parametrize("path", [p for p in HTML_PAGES if p != "/"])
async def test_html_page_requires_auth(anon, path):
    # Unauth pages must not render real data: either 401 or a redirect to login.
    r = await anon.get(path)
    assert r.status_code in (401, 302, 303, 307), f"{path} -> {r.status_code}"


# --- small JSON helpers ----------------------------------------------------

async def test_health_is_public(anon):
    r = await anon.get("/api/health")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


async def test_nav_badges_authed(web):
    r = await web.get("/api/nav-badges")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


async def test_nav_badges_is_public(anon):
    # nav-badges is intentionally reachable without auth (it drives the sidebar
    # badge on every page, including the login screen) — it must not 500.
    r = await anon.get("/api/nav-badges")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


async def test_run_endpoint_without_token_configured(anon):
    # SWITCHBOARD_RUN_TOKEN is unset in the test env → the workflow endpoint is
    # disabled (503), never silently accepts an unauthenticated call.
    r = await anon.post("/run", json={"input": "status"})
    assert r.status_code in (401, 403, 503)


# --- detail pages with bogus ids -------------------------------------------

async def test_unknown_plan_page_404(web):
    assert (await web.get("/plans/99999999")).status_code == 404


async def test_unknown_trend_page_404(web):
    assert (await web.get("/trends/99999999")).status_code == 404
