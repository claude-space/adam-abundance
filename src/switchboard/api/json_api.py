"""JSON API (``/api/*``) for the React/TanStack frontend (story-unraveler-tool).

Same-origin by design: the SPA is served from — or reverse-proxied alongside —
this app, so these endpoints reuse the existing Google-SSO **session cookie**
(no CORS, no tokens). Each returns the SAME real data the server-rendered pages
compute, via shared gatherers in :mod:`routes`, so the HTML and JSON surfaces
never drift. ``require_user`` returns HTTP 401 JSON when unauthenticated.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..config import get_settings
from ..context import RunContext
from . import routes
from .auth import require_user

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    """The signed-in user — lets the SPA render identity + gate admin controls."""
    u = require_user(request)
    return {"email": u.get("email"), "name": u.get("name"), "role": u.get("role"),
            "brands": u.get("brands") or []}


@router.get("/writers")
async def writers(request: Request, brand: str | None = None) -> dict[str, Any]:
    """Writer-emulation data (§16.3): per-brand top-writer leaderboard + versioned
    style profiles — the real backing for the SPA's Writer Emulation screen."""
    u = require_user(request)
    async with RunContext.open() as ctx:
        data = await routes.gather_writer_emulation(ctx, brand)
    data["may_edit"] = u.get("role") in ("global_admin", "portfolio_admin")
    return data


@router.get("/dashboard")
async def dashboard(request: Request) -> dict[str, Any]:
    """The home dashboard: KPI stats, resource usage, agent fleet, brand cards,
    and the recent execution ledger — composed from the same gatherers as the
    server-rendered home page."""
    require_user(request)
    settings = get_settings()
    async with RunContext.open() as ctx:
        plans = [routes._plan_dict(p) for p in await routes.PlanRepo(ctx.session).list_plans(25)]
        portfolio = await routes._portfolio_summary(ctx, list(settings.brand_keys))
        stats = await routes._home_stats(ctx, plans)
        fleet = await routes._agents_overview(ctx)
        spend = await routes._spend_snapshot(ctx)
        deltas = await routes._spend_deltas(ctx, spend)
        resources = await routes._resource_usage(ctx, spend, deltas)
    return {"stats": stats, "resources": resources, "fleet": fleet, "portfolio": portfolio,
            "plans": plans, "spend": spend, "deltas": deltas,
            "brands": list(settings.brand_keys), "kill_switch": settings.kill_switch}


@router.get("/agents")
async def agents(request: Request) -> dict[str, Any]:
    """Agent fleet — per-agent entries/calls/errors + utilization sparkline."""
    require_user(request)
    async with RunContext.open() as ctx:
        return {"agents": await routes._agents_overview(ctx)}


@router.get("/systems")
async def systems(request: Request) -> dict[str, Any]:
    """System Matrix — per-system status + health across the connected tools."""
    require_user(request)
    async with RunContext.open() as ctx:
        groups, health = await routes._system_matrix(ctx)
    return {"groups": groups, "health": health}


@router.get("/activity")
async def activity(request: Request, limit: int = 60) -> dict[str, Any]:
    """Chronological activity feed (tool calls, memory writes, dispatches)."""
    require_user(request)
    async with RunContext.open() as ctx:
        return {"events": await routes._activity_events(ctx, min(max(limit, 1), 200))}


@router.get("/notifications")
async def notifications(request: Request, limit: int = 60) -> dict[str, Any]:
    """Human-actionable items — flags, failed pipelines/jobs, spend-cap hits."""
    require_user(request)
    async with RunContext.open() as ctx:
        return {"items": await routes._notification_items(ctx, min(max(limit, 1), 200))}
