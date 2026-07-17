"""JSON API (``/api/*``) for the React/TanStack frontend (story-unraveler-tool).

Same-origin by design: the SPA is served from — or reverse-proxied alongside —
this app, so these endpoints reuse the existing Google-SSO **session cookie**
(no CORS, no tokens). Each returns the SAME real data the server-rendered pages
compute, via shared gatherers in :mod:`routes`, so the HTML and JSON surfaces
never drift. ``require_user`` returns HTTP 401 JSON when unauthenticated.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

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
    # Header stat pills + the threshold/outlets/dedup config shown in the description.
    # Derived from the same rows so the counts never drift from the tables below.
    tcfg = settings.trends
    stats = {
        "open": sum(1 for t in open_trends if not t.get("pending_pipeline_id")),
        "pending": sum(1 for t in open_trends if t.get("pending_pipeline_id")),
        "generating": sum(1 for p in pipelines if p.get("status") in ("approved", "generating")),
        "previews": sum(1 for p in pipelines if p.get("status") == "previews_ready"),
        "gaps": sum(int(c.get("gaps") or 0) for c in coverage),
    }
    config = {"score_threshold": tcfg.score_threshold, "min_sources": tcfg.min_sources,
              "dedup_days": tcfg.dedup_days}
    return {"open_trends": open_trends, "recent_closed": recent_closed,
            "pipelines": pipelines, "coverage": coverage, "brands": list(settings.brand_keys),
            "stats": stats, "config": config}


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


# --- Trend Radar actions (JSON siblings of the HTML form routes) --------------
# Same business logic, same per-brand RBAC gate (``_can_approve``) as the
# server-rendered routes; the SPA calls these with ``credentials:'include'`` so
# the session cookie carries the caller's role. Approving/scanning spend money
# (LLM + paid source APIs), so they are gated to approvers, never viewers.


@router.post("/trends/scan")
async def trends_scan(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Kick a real trend scan in the background for the caller's brand scope."""
    user = require_user(request)
    body = await _json_body(request)
    brand = (body.get("brand") or "portfolio").strip() or "portfolio"
    settings = get_settings()
    if not settings.is_valid_scope(brand):
        raise HTTPException(status_code=400, detail=f"unknown brand '{brand}'")
    if not routes._can_approve(request, brand):
        raise HTTPException(status_code=403, detail="not permitted for this brand")
    from ..trends.scout import run_trend_scan

    background_tasks.add_task(run_trend_scan, brand)
    return {"ok": True, "scanning": brand}


@router.post("/pipelines/{pipeline_id}/approve")
async def pipeline_approve(request: Request, background_tasks: BackgroundTasks,
                           pipeline_id: int) -> dict[str, Any]:
    """Approve a pending pipeline and start generation (spends money)."""
    user = require_user(request)
    body = await _json_body(request)
    picked = body.get("content_types") or None
    instructions = (body.get("instructions") or "").strip() or None
    from ..trends.lifecycle import LifecycleError
    from ..trends.pipeline import approve_and_start, run_job_sweep
    async with RunContext.open() as ctx:
        pipeline = await routes.PipelineRepo(ctx.session).get(pipeline_id)
        if pipeline is None:
            raise HTTPException(status_code=404, detail="pipeline not found")
        if not routes._can_approve(request, pipeline.brand):
            raise HTTPException(status_code=403, detail="not permitted for this brand")
        try:
            await approve_and_start(ctx, pipeline_id, user["email"],
                                    content_types=picked, instructions=instructions)
        except LifecycleError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    background_tasks.add_task(run_job_sweep)
    return {"ok": True, "pipeline_id": pipeline_id, "status": "approved"}


@router.post("/pipelines/{pipeline_id}/decline")
async def pipeline_decline(request: Request, pipeline_id: int) -> dict[str, Any]:
    """Decline a pending pipeline request and close it out."""
    user = require_user(request)
    body = await _json_body(request)
    reason = (body.get("reason") or "").strip() or None
    from ..trends.lifecycle import LifecycleError
    from ..trends.pipeline import decline_pipeline
    async with RunContext.open() as ctx:
        pipeline = await routes.PipelineRepo(ctx.session).get(pipeline_id)
        if pipeline is None:
            raise HTTPException(status_code=404, detail="pipeline not found")
        if not routes._can_approve(request, pipeline.brand):
            raise HTTPException(status_code=403, detail="not permitted for this brand")
        try:
            await decline_pipeline(ctx, pipeline_id, user["email"], reason)
        except LifecycleError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "pipeline_id": pipeline_id, "status": "declined"}


async def _json_body(request: Request) -> dict[str, Any]:
    """Tolerant JSON-body read — the SPA may POST an empty body for no-arg actions."""
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
