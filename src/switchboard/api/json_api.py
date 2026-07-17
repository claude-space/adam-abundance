"""JSON API (``/api/*``) for the React/TanStack frontend (story-unraveler-tool).

Same-origin by design: the SPA is served from — or reverse-proxied alongside —
this app, so these endpoints reuse the existing Google-SSO **session cookie**
(no CORS, no tokens). Each returns the SAME real data the server-rendered pages
compute, via shared gatherers in :mod:`routes`, so the HTML and JSON surfaces
never drift. ``require_user`` returns HTTP 401 JSON when unauthenticated.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

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


@router.post("/writers/activate")
async def writers_activate(request: Request) -> dict[str, Any]:
    """Set one style-profile version active for its brand (portfolio/global admin
    only) — the JSON sibling of the HTML form action, for the SPA."""
    u = require_user(request)
    if u.get("role") not in ("global_admin", "portfolio_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    try:
        pid = int((await request.json()).get("profile_id"))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="profile_id required")
    from sqlalchemy import select, update as _update

    from ..db.models import WriterStyleProfile
    async with RunContext.open() as ctx:
        prof = (await ctx.session.execute(
            select(WriterStyleProfile).where(WriterStyleProfile.id == pid))).scalar_one_or_none()
        if prof is None:
            raise HTTPException(status_code=404, detail="profile not found")
        await ctx.session.execute(
            _update(WriterStyleProfile)
            .where(WriterStyleProfile.brand == prof.brand, WriterStyleProfile.active.is_(True))
            .values(active=False))
        prof.active = True
        await ctx.session.flush()
        brand, version = prof.brand, prof.version
    return {"ok": True, "brand": brand, "active_version": version}


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


@router.get("/trends")
async def trends(request: Request, brand: str | None = None) -> dict[str, Any]:
    """Trend Radar: open trends ranked by score, recently-closed, the pending
    trigger requests, and per-brand coverage — with per-row approvability."""
    require_user(request)
    settings = get_settings()
    async with RunContext.open() as ctx:
        repo = routes.TrendRepo(ctx.session)
        open_trends = [routes._trend_dict(t) for t in await repo.list(
            brand=brand or None,
            statuses=["detected", "dossier_building", "proposed", "approved"], limit=60)]
        recent_closed = [routes._trend_dict(t) for t in await repo.list(
            brand=brand or None,
            statuses=["dismissed", "declined", "expired", "completed"], limit=15)]
        pipelines = [routes._pipeline_dict(p, with_jobs=False)
                     for p in await routes.PipelineRepo(ctx.session).list(brand=brand or None, limit=40)]
        coverage = await routes._brand_coverage(ctx, list(settings.brand_keys))
    for row in (*open_trends, *recent_closed, *pipelines):
        row["may_approve"] = routes._can_approve(request, row.get("brand", ""))
    return {"open_trends": open_trends, "recent_closed": recent_closed,
            "pipelines": pipelines, "coverage": coverage, "brands": list(settings.brand_keys)}


@router.get("/pipelines")
async def pipelines(request: Request, brand: str | None = None,
                    status: str | None = None) -> dict[str, Any]:
    """Content pipelines (trigger requests → jobs), with status counts."""
    require_user(request)
    async with RunContext.open() as ctx:
        rows = await routes.PipelineRepo(ctx.session).list(
            brand=brand or None, statuses=[status] if status else None, limit=80)
        pipelines = [routes._pipeline_dict(p, with_jobs=False) for p in rows]
    counts: dict[str, int] = {}
    for p in pipelines:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
        p["may_approve"] = routes._can_approve(request, p.get("brand", ""))
    return {"pipelines": pipelines, "counts": counts}
