"""Approval surface + observability routes.

The approval surface (PRD §9) shows the daily plan with ranked items — each with
action, params, rationale, and cost estimate — and lets a human approve / edit /
reject per item, approve the plan, then dispatch. Observability (PRD §12 Phase 5)
exposes spend-vs-caps, a memory browser, and the tool-call audit.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, desc, func, select

from ..adapters.registry import ACTION_REGISTRY, owned_tool_names
from ..config import get_settings
from ..context import RunContext
from ..db.enums import EntryType
from ..db.models import ContentJob, ContentPipeline, MemoryEntry, Plan, PlanItem, ToolCallLog, Trend
from ..logging_ import get_logger
from ..orchestrator.dispatch import Dispatcher
from ..orchestrator.plans import ApprovalError, PlanRepo
from ..rbac import ROLE_LABELS, Role, can_approve, can_manage_users, is_valid_role
from ..trends.lifecycle import CONTENT_TYPES, PIPELINE_OPEN_STATUSES, LifecycleError
from ..trends.pipeline import approve_and_start, decline_pipeline, publish_job, run_job_sweep
from ..trends.repo import PipelineRepo, TrendRepo
from ..users import UserRepo
from .auth import current_user, require_user


def _acting(request: Request) -> tuple[str, list[str]]:
    u = current_user(request) or {}
    return u.get("role", "viewer"), (u.get("brands") or [])


def _can_approve(request: Request, brand: str) -> bool:
    role, brands = _acting(request)
    return can_approve(role, brands, brand)


_FORBIDDEN = JSONResponse({"error": "Your role cannot approve/dispatch this brand"}, status_code=403)

log = get_logger("api.routes")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Base-path handling for the Caddy subpath (/agents/<slug>/) is fully centralized
# in app.py middleware: it rewrites root-absolute href/src/action in rendered HTML
# and prefixes redirect Location headers when APP_BASE_PATH is set. Templates stay
# base-path-agnostic — they emit plain '/...' links and never prefix by hand
# (prefixing here as well would double up).


@router.get("/api/health")
async def api_health():
    """Liveness probe (SHA manifest). Public, no DB required."""
    return JSONResponse({"status": "ok", "agent": "switchboard"})


@router.get("/api/data")
async def api_data():
    """SHA workflow-consumable JSON — aggregates only (no secrets / no raw
    editorial content), matching the manifest's {rows, summary} shape."""
    async with RunContext.open() as ctx:
        by_type = {t.value: int(c) for t, c in (await ctx.session.execute(
            select(MemoryEntry.type, func.count()).where(MemoryEntry.status == "active")
            .group_by(MemoryEntry.type))).all()}
        plans = await PlanRepo(ctx.session).list_plans(15)
        spend = await _spend_snapshot(ctx)
        trend_counts = await TrendRepo(ctx.session).counts_by_status()
        pipeline_rows = await PipelineRepo(ctx.session).list(limit=200)
        rows = [{"id": p.id, "brand": p.brand, "plan_date": p.plan_date.isoformat(),
                 "status": p.status, "items": len(p.items)} for p in plans]
    summary = {
        "active_flags": by_type.get("flag", 0),
        "verified_facts": by_type.get("fact", 0),
        "claims": by_type.get("claim", 0),
        "memory_by_type": by_type,
        "llm_usd_today": round(spend["llm_micros"]["spent_today"] / 1e6, 4),
        "bq_mb_today": round(spend["bq_bytes"]["spent_today"] / 1048576, 1),
        "plans_recent": len(rows),
        "trends_by_status": trend_counts,
        "pipelines_pending_approval": sum(1 for p in pipeline_rows
                                          if p.status == "pending_approval"),
        "pipelines_previews_ready": sum(1 for p in pipeline_rows
                                        if p.status == "previews_ready"),
    }
    return JSONResponse({"rows": rows, "summary": summary})


@router.post("/run")
async def workflow_run(request: Request):
    """ShellAgent Workflow step endpoint (SHA workspace ``CLAUDE.md`` contract).

    Bearer-token auth (``SWITCHBOARD_RUN_TOKEN``); accepts ``{"input": "..."}``
    and returns ``{"output": "<text>"}`` — or ``{"error": ...}`` on failure, per
    the contract. Strictly **read-only**: it summarizes shared-memory + plan +
    spend state and never triggers agents, dispatch, or spend, so it is safe to
    wire into any workflow. If the input names a brand, the summary scopes to it."""
    settings = get_settings()
    token = settings.creds.resolve("SWITCHBOARD_RUN_TOKEN")
    if not token:
        return JSONResponse(
            {"error": "Workflow endpoint not configured (set SWITCHBOARD_RUN_TOKEN)"},
            status_code=503)
    auth = request.headers.get("authorization", "")
    presented = auth[7:] if auth[:7].lower() == "bearer " else ""
    if not presented or not hmac.compare_digest(presented, token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is a client error, not a crash
        body = {}
    query = str((body or {}).get("input", "")).strip()
    ql = query.lower()
    brand = next((k for k, b in settings.brands.items()
                  if k in ql or b.display_name.lower() in ql or b.short_code.lower() in ql), None)
    async with RunContext.open() as ctx:
        by_type = {t.value: int(c) for t, c in (await ctx.session.execute(
            select(MemoryEntry.type, func.count()).where(MemoryEntry.status == "active")
            .group_by(MemoryEntry.type))).all()}
        plans = await PlanRepo(ctx.session).list_plans(50)
        spend = await _spend_snapshot(ctx)
        trend_counts = await TrendRepo(ctx.session).counts_by_status()
        open_pipelines = await PipelineRepo(ctx.session).list(
            statuses=list(PIPELINE_OPEN_STATUSES), limit=100)
    if brand:
        plans = [p for p in plans if p.brand == brand]
        open_pipelines = [p for p in open_pipelines if p.brand == brand]
    scope = settings.brand(brand).display_name if brand else "the Auto portfolio"
    latest = plans[0] if plans else None
    open_trends = sum(v for k, v in trend_counts.items()
                      if k in ("detected", "dossier_building", "proposed", "approved"))
    pending = sum(1 for p in open_pipelines if p.status == "pending_approval")
    previews = sum(1 for p in open_pipelines if p.status == "previews_ready")
    lines = [
        f"Switchboard status for {scope}:",
        f"- Shared memory: {by_type.get('fact', 0)} verified facts, "
        f"{by_type.get('flag', 0)} active flags, {by_type.get('claim', 0)} unverified claims.",
        f"- Plans on record: {len(plans)}" + (
            f"; latest ({latest.plan_date.isoformat()}) is '{latest.status}' "
            f"with {len(latest.items)} items." if latest else "."),
        f"- Competitor trends: {open_trends} open; {pending} pipeline request(s) awaiting "
        f"approval; {previews} with previews ready for review.",
        f"- Spend today: ${spend['llm_micros']['spent_today'] / 1e6:.2f} LLM, "
        f"{spend['bq_bytes']['spent_today'] / 1048576:.0f} MB BigQuery, "
        f"{spend['ahrefs_units']['spent_today']} Ahrefs units.",
    ]
    if settings.kill_switch:
        lines.append("- NOTE: kill switch is ON — observe/plan-only mode (no live actions).")
    return JSONResponse({"output": "\n".join(lines)})


def _plan_dict(p: Plan) -> dict[str, Any]:
    items = [
        {"id": i.id, "rank": i.rank, "assigned_agent": i.assigned_agent,
         "action_type": i.action_type, "params": i.params, "rationale": i.rationale,
         "status": i.status, "dry_run": i.dry_run, "cost_estimate": i.cost_estimate,
         "result_ref": i.result_ref}
        for i in sorted(p.items, key=lambda x: x.rank)
    ]
    totals = {"ahrefs_units": 0, "llm_micros": 0, "bq_bytes": 0}
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
        for k in totals:
            totals[k] += int((it["cost_estimate"] or {}).get(k, 0) or 0)
    return {
        "id": p.id, "brand": p.brand, "plan_date": p.plan_date.isoformat(), "status": p.status,
        "created_by": p.created_by, "approved_by": p.approved_by,
        "approved_at": p.approved_at.isoformat() if p.approved_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "items": items, "cost_totals": totals, "status_counts": counts,
    }


# ---------------------------------------------------------------------------
# Approval surface
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    settings = get_settings()
    user = current_user(request)
    if not user:
        return templates.TemplateResponse(request, "login.html", {"env": settings.env})
    async with RunContext.open() as ctx:
        plans = [_plan_dict(p) for p in await PlanRepo(ctx.session).list_plans(25)]
        portfolio = await _portfolio_summary(ctx, list(settings.brand_keys))
        stats = await _home_stats(ctx, plans)
        fleet = await _agents_overview(ctx)
        spend = await _spend_snapshot(ctx)
        deltas = await _spend_deltas(ctx, spend)
        resources = await _resource_usage(ctx, spend, deltas)
    now = datetime.now()
    greeting = "Good morning" if now.hour < 12 else ("Good afternoon" if now.hour < 18 else "Good evening")
    first = (user.get("name") or user.get("email", "")).split("@")[0].split(".")[0].title()
    hero = {"greeting": greeting, "name": first, "date": f"{now:%A, %B} {now.day}, {now:%Y}"}
    fleet_max = max((a["entries"] for a in fleet), default=1) or 1
    caps = settings.caps
    _llm_cap, _bq_cap, _ah_cap = caps.per_day("llm_micros"), caps.per_day("bq_bytes"), caps.per_day("ahrefs_units")
    gauges = [
        {"label": "LLM spend", "gc": "#5b9dff", "pct": spend["llm_micros"]["pct"] or 0,
         "val": f"${spend['llm_micros']['spent_today']/1e6:.2f}",
         "cap": f"of ${_llm_cap/1e6:.2f} cap" if _llm_cap else "no cap set"},
        {"label": "BigQuery", "gc": "#3fb950", "pct": spend["bq_bytes"]["pct"] or 0,
         "val": f"{spend['bq_bytes']['spent_today']/1048576:.0f} MB",
         "cap": f"of {_bq_cap/1024**3:.0f} GiB cap" if _bq_cap else "no cap set"},
        {"label": "Ahrefs units", "gc": "#d6a021", "pct": spend["ahrefs_units"]["pct"] or 0,
         "val": f"{spend['ahrefs_units']['spent_today']}",
         "cap": f"of {_ah_cap} cap" if _ah_cap else "no cap set"},
    ]
    return templates.TemplateResponse(
        request, "plans.html",
        {"user": user, "plans": plans, "brands": list(settings.brand_keys),
         "kill_switch": settings.kill_switch, "portfolio": portfolio, "stats": stats, "fleet": fleet,
         "hero": hero, "gauges": gauges, "fleet_max": fleet_max, "caps_enabled": caps.enabled,
         "deltas": deltas, "resources": resources},
    )


@router.get("/overview", response_class=HTMLResponse)
async def overview(request: Request):
    """"How it works" — a static orientation page. No DB, so it renders even on a
    fresh deploy. The agent fleet is driven by AGENT_META (same source as /agents)."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    settings = get_settings()
    return templates.TemplateResponse(
        request, "overview.html",
        {"user": user, "kill_switch": settings.kill_switch, "agents": AGENT_META,
         "caps_enabled": settings.caps.enabled, "brands": list(settings.brand_keys)},
    )


@router.post("/cycle")
async def trigger_cycle(request: Request, brand: str = Form(...)):
    require_user(request)
    from ..orchestrator import run_morning_cycle

    settings = get_settings()
    brands = list(settings.brand_keys) if brand == "all" else [brand]
    for b in brands:
        await run_morning_cycle(b)
    target = brands[0]
    async with RunContext.open() as ctx:
        latest = await PlanRepo(ctx.session).latest_plan(target, date.today())
        plan_id = latest.id if latest else None
    return RedirectResponse(f"/plans/{plan_id}" if plan_id else "/", status_code=302)


@router.get("/plans/{plan_id}", response_class=HTMLResponse)
async def plan_detail(request: Request, plan_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    async with RunContext.open() as ctx:
        plan = await PlanRepo(ctx.session).get_plan(plan_id)
        data = _plan_dict(plan) if plan else None
    if data is None:
        return HTMLResponse("<h3>Plan not found</h3>", status_code=404)
    may_approve = _can_approve(request, data["brand"])
    return templates.TemplateResponse(request, "plan_detail.html",
                                      {"user": user, "plan": data, "can_approve": may_approve})


@router.post("/plans/{plan_id}/approve")
async def approve_plan(request: Request, plan_id: int):
    user = require_user(request)
    async with RunContext.open() as ctx:
        repo = PlanRepo(ctx.session)
        plan = await repo.get_plan(plan_id)
        if plan is None:
            return JSONResponse({"error": "plan not found"}, status_code=404)
        if not _can_approve(request, plan.brand):
            return _FORBIDDEN
        try:
            await repo.approve_plan(plan_id, user["email"])
        except ApprovalError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return RedirectResponse(f"/plans/{plan_id}", status_code=302)


@router.post("/items/{item_id}/approve")
async def approve_item(request: Request, item_id: int, plan_id: int = Form(...), go_live: str = Form("")):
    user = require_user(request)
    live = go_live.lower() in ("1", "true", "on", "yes")
    async with RunContext.open() as ctx:
        repo = PlanRepo(ctx.session)
        plan = await repo.get_plan(plan_id)
        if plan and not _can_approve(request, plan.brand):
            return _FORBIDDEN
        try:
            await repo.approve_item(item_id, user["email"], go_live=live)
        except ApprovalError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return RedirectResponse(f"/plans/{plan_id}", status_code=302)


@router.post("/items/{item_id}/reject")
async def reject_item(request: Request, item_id: int, plan_id: int = Form(...)):
    user = require_user(request)
    async with RunContext.open() as ctx:
        repo = PlanRepo(ctx.session)
        plan = await repo.get_plan(plan_id)
        if plan and not _can_approve(request, plan.brand):
            return _FORBIDDEN
        await repo.reject_item(item_id, user["email"])
    return RedirectResponse(f"/plans/{plan_id}", status_code=302)


@router.post("/plans/{plan_id}/approve-all")
async def approve_all(request: Request, plan_id: int):
    """Approve the plan and every still-proposed item, all dry-run (no go-live)."""
    user = require_user(request)
    async with RunContext.open() as ctx:
        repo = PlanRepo(ctx.session)
        plan = await repo.get_plan(plan_id)
        if plan is None:
            return JSONResponse({"error": "plan not found"}, status_code=404)
        if not _can_approve(request, plan.brand):
            return _FORBIDDEN
        try:
            await repo.approve_plan(plan_id, user["email"])
            for it in plan.items:
                if it.status == "proposed":
                    await repo.approve_item(it.id, user["email"], go_live=False)
        except ApprovalError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return RedirectResponse(f"/plans/{plan_id}", status_code=302)


@router.post("/items/{item_id}/edit")
async def edit_item(request: Request, item_id: int, plan_id: int = Form(...),
                    rationale: str = Form(""), params_json: str = Form("")):
    require_user(request)
    import json

    params = None
    if params_json.strip():
        try:
            params = json.loads(params_json)
        except ValueError:
            return JSONResponse({"error": "params must be valid JSON"}, status_code=400)
    async with RunContext.open() as ctx:
        repo = PlanRepo(ctx.session)
        plan = await repo.get_plan(plan_id)
        if plan and not _can_approve(request, plan.brand):
            return _FORBIDDEN
        await repo.edit_item(item_id, params=params, rationale=rationale or None)
    return RedirectResponse(f"/plans/{plan_id}", status_code=302)


@router.post("/plans/{plan_id}/dispatch")
async def dispatch_plan(request: Request, plan_id: int):
    require_user(request)
    async with RunContext.open() as ctx:
        plan = await PlanRepo(ctx.session).get_plan(plan_id)
        if plan is None:
            return JSONResponse({"error": "plan not found"}, status_code=404)
        if not _can_approve(request, plan.brand):
            return _FORBIDDEN
        summary = await Dispatcher(ctx).dispatch_plan(plan_id)
    return templates.TemplateResponse(
        request, "dispatch_result.html", {"summary": summary, "plan_id": plan_id}
    )


# ---------------------------------------------------------------------------
# Observability (Phase 5)
# ---------------------------------------------------------------------------

@router.get("/memory", response_class=HTMLResponse)
async def memory_browser(request: Request, brand: str | None = None, type: str | None = None,
                         verified: str | None = None, source_agent: str | None = None, limit: int = 100):
    """Provenance-first memory browser: filter by brand/type/verified/agent and
    inspect each entry's source + payload (PRD §12 Phase 5 observability)."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    settings = get_settings()
    vfilter = {"true": True, "false": False}.get((verified or "").lower())
    types = [EntryType(type)] if type else None
    async with RunContext.open() as ctx:
        rows = await ctx.store.query(brand=brand or None, include_portfolio=True, types=types,
                                     verified=vfilter, source_agent=source_agent or None,
                                     status=None, limit=min(limit, 500))
        entries = [{"id": r.id, "type": r.type.value, "brand": r.brand, "source_agent": r.source_agent,
                    "source_system": r.source_system, "verified": r.verified, "confidence": r.confidence,
                    "source_urls": r.source_urls or [], "status": r.status,
                    "kind": (r.payload or {}).get("kind", ""),
                    "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
                    "payload": r.payload} for r in rows]
        # Analytical summary for the page hero: active counts by type + fact/claim split.
        counts = (await ctx.session.execute(
            select(MemoryEntry.type, func.count()).where(MemoryEntry.status == "active")
            .group_by(MemoryEntry.type))).all()
        by_type = {t.value: int(c) for t, c in counts}
        verified_ct = int((await ctx.session.execute(
            select(func.count()).where(MemoryEntry.verified.is_(True), MemoryEntry.status == "active")
        )).scalar_one())
        summary = {"total": sum(by_type.values()), "by_type": by_type, "verified": verified_ct,
                   "facts": by_type.get("fact", 0), "claims": by_type.get("claim", 0)}
    return templates.TemplateResponse(
        request, "memory.html",
        {"user": user, "entries": entries, "summary": summary, "brands": list(settings.brand_keys),
         "types": [t.value for t in EntryType],
         "agents": ["research", "opportunity", "production", "analytics", "reporting", "paid_media",
                    "orchestrator", "decay_scan", "content_audit", "trend_scout", "trend_pipeline",
                    "governor", "system"],
         "f": {"brand": brand or "", "type": type or "", "verified": verified or "",
               "source_agent": source_agent or ""}},
    )


@router.get("/memory/{entry_id}", response_class=HTMLResponse)
async def memory_entry_detail(request: Request, entry_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    async with RunContext.open() as ctx:
        row = await ctx.session.get(MemoryEntry, entry_id)
        if row is None:
            return HTMLResponse("<h3>Entry not found</h3>", status_code=404)
        entry = {"id": row.id, "type": row.type.value, "brand": row.brand,
                 "source_agent": row.source_agent, "source_system": row.source_system,
                 "verified": row.verified, "confidence": row.confidence,
                 "source_urls": row.source_urls or [], "status": row.status,
                 "kind": (row.payload or {}).get("kind", ""),
                 "created_at": row.created_at.strftime("%Y-%m-%d %H:%M UTC") if row.created_at else "",
                 "expires_at": row.expires_at.strftime("%Y-%m-%d %H:%M UTC") if row.expires_at else None,
                 "payload": row.payload}
        # A few sibling entries of the same brand+kind for context.
        related = await ctx.store.query(brand=row.brand, types=[row.type],
                                        payload_contains={"kind": entry["kind"]} if entry["kind"] else None,
                                        status=None, limit=6)
        siblings = [{"id": r.id, "source_agent": r.source_agent, "status": r.status,
                     "created_at": r.created_at.strftime("%m-%d %H:%M") if r.created_at else ""}
                    for r in related if r.id != row.id][:5]
    return templates.TemplateResponse(request, "memory_entry.html",
                                      {"user": user, "e": entry, "siblings": siblings})


# The fleet (PRD §6): 6 workers + the orchestrator. Coordinate ONLY via memory.
AGENT_META: list[dict[str, Any]] = [
    {"key": "orchestrator", "display": "Orchestrator", "tagline": "Chief of staff", "color": "#7c5cff",
     "domain": "No domain work. Each morning: read shared memory → synthesize a prioritized plan → human "
               "approval → dispatch. Holds the governor on the dispatch path.",
     "outputs": "plan · plan_item · decision"},
    {"key": "research", "display": "Research", "tagline": "Outside-in + fact gate", "color": "#26c6a6",
     "domain": "Pre-fetches market/news/competitor context and is the only agent that can certify a verified "
               "fact — search-confirmed, or it stays a claim.",
     "outputs": "fact · claim · metric · flag"},
    {"key": "opportunity", "display": "Opportunity", "tagline": "What to make next", "color": "#5b9dff",
     "domain": "Keyword-gap mining, competitor angles, own winners, and viral-trend surfacing. Proposes topics "
               "and triggers Albert / Seona / HC-Viral ideation.",
     "outputs": "context (candidates) · metric · flag"},
    {"key": "production", "display": "Production", "tagline": "Pipeline state", "color": "#d6a021",
     "domain": "Tracks both writing pipelines + Asana; flags bottlenecks and stuck outlines. Routes topics to "
               "writers and pushes CMS drafts — governor-gated, dry-run by default.",
     "outputs": "metric · flag · action results"},
    {"key": "analytics", "display": "Analytics", "tagline": "How we did", "color": "#3fb950",
     "domain": "Published performance, sessions, and writer pace from BigQuery (consum + ODS), Sentinel, and "
               "Sheets. The performance brain.",
     "outputs": "ranked metric · flag"},
    {"key": "reporting", "display": "Reporting & Distribution", "tagline": "What goes out", "color": "#ec6cb0",
     "domain": "Assembles the daily digest, CarBuzz newsletter, and social posts from performance data — for "
               "human review. Never sends or posts autonomously.",
     "outputs": "report · distribution_draft · flag"},
    {"key": "paid_media", "display": "Paid-Media", "tagline": "Spend & ROI", "color": "#e8833a",
     "domain": "Ad spend, conversions, and ROI for the [CB] -M- marketplace campaigns. Strictly read-only — "
               "never touches bids, budgets, or campaigns.",
     "outputs": "metric (domain:paid_media) · flag"},
]
AGENT_COLORS = {m["key"]: m["color"] for m in AGENT_META}
AGENT_COLORS.update({"decay_scan": "#6f7887", "content_audit": "#6f7887", "governor": "#f4574d",
                     "system": "#6f7887", "trend_scout": "#26c6a6", "trend_pipeline": "#26c6a6"})
# Entry-type identity colors (memory browser, donuts, badges).
TYPE_COLORS = {"metric": "#5b9dff", "flag": "#d6a021", "fact": "#3fb950", "claim": "#e8833a",
               "decision": "#7c5cff", "context": "#6f7887", "report": "#ec6cb0",
               "distribution_draft": "#26c6a6", "plan_item": "#8ea1b8"}
# Make the color lookups available to every template.
templates.env.globals["agent_color"] = lambda k: AGENT_COLORS.get(k, "#6f7887")
templates.env.globals["type_color"] = lambda t: TYPE_COLORS.get(t, "#6f7887")
# Service logo via logo.dev (needs a publishable token); "" → template shows a monogram.
templates.env.globals["logo_url"] = lambda domain: (
    f"https://img.logo.dev/{domain}?token={get_settings().logo_dev_token}&size=64&format=png&retina=true"
    if get_settings().logo_dev_token and domain else ""
)

# OEM (automaker) → domain for logo.dev, keyed lowercase to match the detector's
# lowercased OEM names (switchboard.trends.detector). Covers the detector brand list.
_OEM_DOMAINS: dict[str, str] = {
    "acura": "acura.com", "alfa romeo": "alfaromeo.com", "aston martin": "astonmartin.com",
    "audi": "audi.com", "bentley": "bentleymotors.com", "bmw": "bmw.com", "bugatti": "bugatti.com",
    "buick": "buick.com", "byd": "byd.com", "cadillac": "cadillac.com", "chevrolet": "chevrolet.com",
    "chevy": "chevrolet.com", "chrysler": "chrysler.com", "citroen": "citroen.com", "dodge": "dodge.com",
    "ducati": "ducati.com", "ferrari": "ferrari.com", "fiat": "fiat.com", "fisker": "fiskerinc.com",
    "ford": "ford.com", "genesis": "genesis.com", "gm": "gm.com", "gmc": "gmc.com",
    "harley-davidson": "harley-davidson.com", "honda": "honda.com", "hyundai": "hyundai.com",
    "infiniti": "infinitiusa.com", "jaguar": "jaguar.com", "jeep": "jeep.com", "kia": "kia.com",
    "koenigsegg": "koenigsegg.com", "lamborghini": "lamborghini.com", "land rover": "landrover.com",
    "lexus": "lexus.com", "lincoln": "lincoln.com", "lotus": "lotuscars.com", "lucid": "lucidmotors.com",
    "maserati": "maserati.com", "mazda": "mazda.com", "mclaren": "mclaren.com",
    "mercedes": "mercedes-benz.com", "mini": "mini.com", "mitsubishi": "mitsubishicars.com",
    "nio": "nio.com", "nissan": "nissanusa.com", "pagani": "pagani.com", "peugeot": "peugeot.com",
    "polestar": "polestar.com", "porsche": "porsche.com", "ram": "ramtrucks.com", "renault": "renault.com",
    "rimac": "rimac-automobili.com", "rivian": "rivian.com", "rolls-royce": "rolls-roycemotorcars.com",
    "scout": "scoutmotors.com", "skoda": "skoda-auto.com", "stellantis": "stellantis.com",
    "subaru": "subaru.com", "suzuki": "suzuki.com", "tesla": "tesla.com", "toyota": "toyota.com",
    "vinfast": "vinfastauto.com", "volkswagen": "vw.com", "volvo": "volvocars.com",
    "xiaomi": "xiaomi.com", "yamaha": "yamaha-motor.com",
}
templates.env.globals["oem_domain"] = lambda name: _OEM_DOMAINS.get((name or "").strip().lower(), "")

FEEDER_META = [
    {"key": "decay_scan", "display": "Ranking-decay scan",
     "domain": "Seona's scanner drops decay candidates into memory on its schedule."},
    {"key": "content_audit", "display": "Content-depth auditor",
     "domain": "Flags thin / low-depth articles into memory on its schedule."},
    {"key": "trend_scout", "display": "Trend Scout",
     "domain": "Clusters competitor coverage + Tavily/Perplexity/Firecrawl/NewsAPI signals into "
               "scored trends and files pipeline trigger requests for human approval."},
]


async def _agents_overview(ctx: RunContext) -> list[dict[str, Any]]:
    mem_rows = (await ctx.session.execute(
        select(MemoryEntry.source_agent, func.count()).where(MemoryEntry.status == "active")
        .group_by(MemoryEntry.source_agent)
    )).all()
    mem = {a: int(c) for a, c in mem_rows}
    call_rows = (await ctx.session.execute(
        select(ToolCallLog.agent, func.count(), func.max(ToolCallLog.created_at)).group_by(ToolCallLog.agent)
    )).all()
    calls = {a: (int(c), ts) for a, c, ts in call_rows}
    err_rows = (await ctx.session.execute(
        select(ToolCallLog.agent, func.count()).where(ToolCallLog.ok.is_(False))
        .group_by(ToolCallLog.agent)
    )).all()
    errors = {a: int(c) for a, c in err_rows}
    out = []
    for m in AGENT_META:
        k = m["key"]
        actions = sorted(at for at, cls in ACTION_REGISTRY.items() if cls.owner_agent == k)
        if k == "orchestrator":
            actions = ["notify", *actions]
        c, last = calls.get(k, (0, None))
        out.append({**m, "owns": owned_tool_names(k), "actions": actions, "read_only": not actions,
                    "entries": mem.get(k, 0), "calls": c, "errors": errors.get(k, 0),
                    "last_active": last.strftime("%m-%d %H:%M") if last else None})
    return out


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    async with RunContext.open() as ctx:
        agents = await _agents_overview(ctx)
        feeders = []
        for f in FEEDER_META:
            n = int((await ctx.session.execute(
                select(func.count()).where(MemoryEntry.source_agent == f["key"])
            )).scalar_one())
            feeders.append({**f, "entries": n})
    return templates.TemplateResponse(request, "agents.html",
                                      {"user": user, "agents": agents, "feeders": feeders})


@router.get("/agents/{name}", response_class=HTMLResponse)
async def agent_detail(request: Request, name: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    meta = next((m for m in AGENT_META if m["key"] == name), None)
    if meta is None:
        return HTMLResponse("<h3>Unknown agent</h3>", status_code=404)
    actions = sorted(at for at, cls in ACTION_REGISTRY.items() if cls.owner_agent == name)
    if name == "orchestrator":
        actions = ["notify", *actions]
    async with RunContext.open() as ctx:
        rows = await ctx.store.query(source_agent=name, status=None, limit=25)
        entries = [{"id": r.id, "type": r.type.value, "brand": r.brand,
                    "kind": (r.payload or {}).get("kind", ""), "status": r.status,
                    "verified": r.verified,
                    "created_at": r.created_at.strftime("%m-%d %H:%M") if r.created_at else ""} for r in rows]
        tcalls = await _recent_audit_for(ctx, name, 20)
    ok_calls = sum(1 for c in tcalls if c["ok"])
    stats = {"entries": len(entries), "calls": len(tcalls),
             "ok_rate": round(100 * ok_calls / len(tcalls)) if tcalls else None,
             "last": tcalls[0]["created_at"] if tcalls else None}
    return templates.TemplateResponse(request, "agent_detail.html",
                                      {"user": user, "m": meta, "owns": owned_tool_names(name),
                                       "actions": actions, "read_only": not actions,
                                       "entries": entries, "calls": tcalls, "stats": stats})


async def _recent_audit_for(ctx: RunContext, agent: str, limit: int) -> list[dict[str, Any]]:
    rows = (await ctx.session.execute(
        select(ToolCallLog).where(ToolCallLog.agent == agent)
        .order_by(desc(ToolCallLog.created_at)).limit(limit)
    )).scalars().all()
    return [{"tool": r.tool, "action": r.action, "brand": r.brand, "dry_run": r.dry_run, "ok": r.ok,
             "created_at": r.created_at.strftime("%m-%d %H:%M") if r.created_at else ""} for r in rows]


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    if not can_manage_users(user.get("role", "viewer")):
        return HTMLResponse("<h3>Forbidden — global admin only.</h3>", status_code=403)
    settings = get_settings()
    async with RunContext.open() as ctx:
        rows = await UserRepo(ctx.session).list()
        users = [{"email": u.email, "role": u.role, "brands": u.brands or [], "name": u.name,
                  "created_at": u.created_at.strftime("%Y-%m-%d") if u.created_at else ""} for u in rows]
    role_counts: dict[str, int] = {}
    for u in users:
        role_counts[u["role"]] = role_counts.get(u["role"], 0) + 1
    return templates.TemplateResponse(request, "users.html",
                                      {"user": user, "users": users, "roles": [r.value for r in Role],
                                       "role_labels": ROLE_LABELS, "brands": list(settings.brand_keys),
                                       "role_counts": role_counts})


@router.post("/users/set-role")
async def set_role_route(request: Request, email: str = Form(...), role: str = Form(...),
                         brands: str = Form("")):
    actor = current_user(request)
    if not actor:
        return RedirectResponse("/auth/login", status_code=302)
    if not can_manage_users(actor.get("role", "viewer")):
        return _FORBIDDEN
    if not is_valid_role(role):
        return JSONResponse({"error": "invalid role"}, status_code=400)
    brand_list = [b.strip() for b in brands.split(",") if b.strip()] or None
    async with RunContext.open() as ctx:
        try:
            await UserRepo(ctx.session).set_role(email, role, brand_list)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return RedirectResponse("/users", status_code=302)


# The wrapped systems + external tools (PRD §4). `check` is (kind, key):
#   cred  -> credentials.describe() presence   env -> a specific env var present
#   service  -> a base URL that IS live-probed each load (online / offline)
#   endpoint -> a configured base URL (shown, not probed)   always -> built-in
SYSTEMS_META: list[dict[str, Any]] = [
    # Warehouse & data
    ("Warehouse & data", "BigQuery", "analytics", "read", "PubInsights consum + ODS article analysis", ("cred", "google_sa_inline")),
    ("Warehouse & data", "Sentinel Pro", "analytics", "read", "Day-of sessions/engagement + conversion events", ("cred", "sentinel")),
    ("Warehouse & data", "Google Sheets", "analytics", "read", "Writer quotas + paid-media RAW_DATA log", ("cred", "google_sa_inline")),
    ("Warehouse & data", "Ahrefs", "opportunity", "read · metered", "Competitor keywords, SERP, backlinks", ("cred", "ahrefs")),
    ("Warehouse & data", "GSC exports", "opportunity", "read", "Search demand (empty for Auto trio — §13.13)", ("cred", "google_sa_inline")),
    ("Warehouse & data", "Similarweb", "research", "read", "Competitor traffic estimates", ("cred", "similarweb")),
    # Trend sourcing (docs/trend-pipeline.md)
    ("Trend sourcing", "Tavily", "research", "read", "News search — trend signals + dossier deep search", ("cred", "tavily")),
    ("Trend sourcing", "Perplexity", "research", "read", "Search-grounded summaries with citations", ("cred", "perplexity")),
    ("Trend sourcing", "Firecrawl", "research", "read", "Web search + page extraction for dossiers", ("cred", "firecrawl")),
    ("Trend sourcing", "NewsAPI", "research", "read", "Headline stream for the trend scout", ("cred", "newsapi")),
    # Editorial pipelines
    ("Editorial pipelines", "Claude Albert", "production", "read + action", "Discover ideation + AI writer + outline reviewer", ("service", "albert")),
    ("Editorial pipelines", "Seona", "opportunity", "read + action", "SEO ideation + ranking-decay + Update Strategist", ("service", "seona")),
    ("Editorial pipelines", "HC Viral Hits", "production", "read + action", "Viral ideation + AI writer + Emaki CMS push", ("service", "hc_viral_hits")),
    ("Editorial pipelines", "Asana", "production", "read + action", "Tasks + outline-approval workflow", ("cred", "asana")),
    ("Editorial pipelines", "Emaki CMS", "production", "action · gated", "Push unpublished article drafts (via HC-Viral)", ("env", "HC_VIRAL_HITS_API_KEY")),
    ("Editorial pipelines", "content-depth-auditor", "analytics", "feeder", "Content-depth findings → memory", ("env", "CONTENT_AUDITOR_URL")),
    ("Editorial pipelines", "writers-dashboard", "analytics", "read", "Writer-performance metric logic (superseded)", ("service", "writers_dashboard")),
    # Distribution
    ("Distribution", "daily-reporting-agent", "reporting", "read + send", "Per-brand editorial email digest", ("cred", "gmail")),
    ("Distribution", "newsletter-creator-auto", "reporting", "action · assemble", "CarBuzz newsletter draft (HTML)", ("env", "NEWSLETTER_API_URL")),
    ("Distribution", "social-media-posts-creator", "reporting", "action · assemble", "Social images + captions", ("env", "SOCIAL_API_URL")),
    ("Distribution", "Gmail API", "reporting", "action · gated", "Send digest (gmail.send-scoped)", ("cred", "gmail")),
    # Paid media (read-only)
    ("Paid media", "Google Ads", "paid_media", "read-only", "Marketplace [CB] -M- campaign spend", ("cred", "google_ads")),
    ("Paid media", "Meta Ads", "paid_media", "read-only", "Campaign spend / clicks", ("cred", "facebook_ads")),
    ("Paid media", "Bing Ads", "paid_media", "read-only", "Campaign spend (Google-OAuth login)", ("cred", "bing_ads")),
    ("Paid media", "Lead feeds", "paid_media", "read-only", "Lotlinx / Carzing / Cars&Bids conversions", ("cred", "lotlinx")),
    ("Paid media", "mp-spend RAW_DATA", "paid_media", "read-only", "Authoritative spend/ROI sheet", ("cred", "google_sa_inline")),
    # Substrate & platform
    ("Substrate & platform", "Anthropic (Claude)", "orchestrator", "substrate", "LLM for all LLM-backed agents", ("cred", "anthropic")),
    ("Substrate & platform", "Slack", "orchestrator", "notify", "Brief + notifications (notify-only)", ("env", "SLACK_BOT_TOKEN")),
    ("Substrate & platform", "Google OAuth", "orchestrator", "auth", "Login + approval attribution", ("cred", "google_oauth")),
    ("Substrate & platform", "PostgreSQL", "orchestrator", "memory", "Shared-memory coordination substrate", ("cred", "database_url")),
]

CONSOLIDATION = [  # PRD §15 — surfaced, not assumed; annotated as decisions land
    "Two BigQuery article tables (ODS vs consum) feed different systems for the same brands. "
    "→ Resolved: consum (pubinsights_consum_data) = published performance, ODS (pubinsights_ods_data) "
    "= Discover; confirmed with Artem — kept separate by purpose.",
    "Two ideation + AI-writer pipelines (Claude Albert / HC Viral Hits) draft in parallel with "
    "overlapping brands. → Resolved: de-dup enabled in both Albert and HC Viral Hits.",
    "Two performance-digest paths (writers-dashboard vs daily-reporting) compute overlapping per-brand "
    "performance. → Resolved: writers-dashboard tracks writer performance only; daily-reporting-agent "
    "pulls the top live articles.",
    "Two cost-tracking schemes (Albert cost_micros vs HC-Viral compute_cost_cents). → Decided: route "
    "both into the governor's spend_ledger (unit already unified in costs.py); pending a cost/usage "
    "endpoint from Albert + HC-Viral to read from.",
    "Two per-brand trend monitors (Switchboard Trend Scout vs HC Viral Hits). → Decided: run both; a "
    "competitor-trend gets a cross-monitor bonus (+15) when HC-Viral independently landed on the same "
    "topic. Corroboration reads HC-Viral's ready drafts today; widens to all topics once it exposes "
    "/api/cms/topics.",
]

DECISIONS = [  # PRD §13 — what I defaulted vs. what's still open
    ("Approval surface (§13.3)", "default", "Built the web view; Slack buttons still acceptable for MVP."),
    ("Cross-brand scope (§13.7)", "default", "All three brands enabled; verified end-to-end on HotCars."),
    ("Trigger vs read on ideation (§13.6)", "default", "Both — read adapters + governor-gated triggers, dry-run by default."),
    ("Canonical BQ table (§13.8)", "default", "consum for published performance, ODS for Discover — both wired; reconciliation TBD."),
    ("Spend cap numbers (§13.4)", "default", "Config defaults (5000 units / $20 LLM / 100 GiB BQ) — confirm real limits."),
    ("Bing accounts/endpoints (§13.1–2)", "open", "Deferred — no confirmed account; adapter degrades softly."),
    ("GSC table population (§13.13)", "open", "Auto-trio gsc tables empty — who populates them?"),
    ("Emaki storage-state refresh (§13.10)", "open", "Playwright session expires — refresh owner + alerting undecided."),
    ("Digest sender identity (§13.11)", "open", "Defaulting to anthony.a@ via Gmail — confirm sender + Slack path."),
    ("Where Switchboard runs (§13.5)", "open", "Local dev now; deployment target vs. existing services undecided."),
]


# --- live service health probing (Systems page) -----------------------------
# `service` rows carry a base URL we actually ping on load, so the console can
# report "online / offline" instead of merely "a URL is configured". Results are
# cached briefly so refreshes stay snappy and we don't hammer the services.
_PROBE_TTL_SEC = 20.0
_PROBE_TIMEOUT_SEC = 1.5
_probe_cache: dict[str, tuple[float, bool]] = {}


async def _probe_one(url: str) -> bool:
    """True if the service answers *any* HTTP response (even 4xx/5xx) within the
    timeout — that means it is up. Refused / timeout / DNS failure → offline."""
    try:
        import httpx
    except ImportError:  # httpx is an adapter extra; degrade to "can't confirm"
        return False
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SEC, follow_redirects=False) as client:
            await client.get(url)
        return True
    except Exception:  # noqa: BLE001 — any transport error means unreachable
        return False


async def _probe_services(urls: dict[str, str]) -> dict[str, bool]:
    """Probe ``{endpoint_key: url}`` concurrently, honouring a short TTL cache."""
    now = time.monotonic()
    result: dict[str, bool] = {}
    todo: dict[str, str] = {}
    for key, url in urls.items():
        cached = _probe_cache.get(key)
        if cached and (now - cached[0]) < _PROBE_TTL_SEC:
            result[key] = cached[1]
        else:
            todo[key] = url
    if todo:
        probed = await asyncio.gather(*(_probe_one(u) for u in todo.values()))
        for key, ok in zip(todo.keys(), probed):
            _probe_cache[key] = (now, ok)
            result[key] = ok
    return result


# System name -> logo.dev domain (absent -> a 2-letter monogram is shown). Internal
# Valnet agents/services have no public domain and fall back to the monogram.
_SYSTEM_DOMAINS: dict[str, str] = {
    "BigQuery": "cloud.google.com", "Google Sheets": "google.com", "GSC exports": "google.com",
    "Ahrefs": "ahrefs.com", "Similarweb": "similarweb.com",
    "Tavily": "tavily.com", "Perplexity": "perplexity.ai", "Firecrawl": "firecrawl.dev",
    "NewsAPI": "newsapi.org", "Asana": "asana.com", "Gmail API": "google.com",
    "Google Ads": "google.com", "Meta Ads": "meta.com", "Bing Ads": "bing.com",
    "Anthropic (Claude)": "anthropic.com", "Slack": "slack.com", "Google OAuth": "google.com",
    "PostgreSQL": "postgresql.org",
}

# System name -> the tool_call_log.tool names that represent it, for real 24h
# volume / errors / success. Systems absent here have no mapped tools and read
# "idle" (no usage) — honest for a low-traffic surface.
_SYSTEM_TOOLS: dict[str, list[str]] = {
    "BigQuery": ["bigquery_consum", "bigquery_discover"],
    "Sentinel Pro": ["sentinel_traffic"],
    "Google Sheets": ["sheets_quota"],
    "Ahrefs": ["ahrefs_keywords", "ahrefs_serp", "ahrefs_backlinks"],
    "Similarweb": ["similarweb_traffic"],
    "Tavily": ["tavily_trends", "tavily_deep_search"],
    "Perplexity": ["perplexity_trends"],
    "Firecrawl": ["firecrawl_trends", "firecrawl_scrape"],
    "NewsAPI": ["newsapi_trends"],
}

# System name -> metered spend metric, for the real cap-usage bar (spend_ledger).
_SYSTEM_METRIC: dict[str, str] = {
    "Anthropic (Claude)": "llm_micros", "BigQuery": "bq_bytes", "Ahrefs": "ahrefs_units",
}


def _fmt_bytes(n: int) -> str:
    n = float(int(n or 0))
    for unit, size in (("GiB", 1073741824), ("MiB", 1048576), ("KiB", 1024)):
        if n >= size:
            v = n / size
            return f"{v:.1f} {unit}" if v < 100 else f"{v:.0f} {unit}"
    return f"{int(n)} B"


async def _system_matrix(ctx: RunContext) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Per-system usage + health, all real: reachability from the live service
    probe, cap usage from spend_ledger (LLM/BigQuery/Ahrefs), and 24h volume /
    errors / success from the tool_call_log audit trail. Returns the systems
    grouped by category plus a health tally for the hero."""
    from datetime import datetime as _dt, timedelta, timezone as _tz

    creds = ctx.creds
    present = creds.describe()
    endpoints = ctx.settings.endpoints

    # Live-probe every service row that actually has a URL, concurrently.
    service_urls = {
        key: endpoints[key]
        for _c, _n, _o, _a, _u, (kind, key) in SYSTEMS_META
        if kind == "service" and endpoints.get(key)
    }
    reachable = await _probe_services(service_urls) if service_urls else {}

    # One pass over the last 24h of tool calls, grouped by tool.
    since = _dt.now(_tz.utc) - timedelta(hours=24)
    agg_rows = (await ctx.session.execute(
        select(ToolCallLog.tool, func.count(),
               func.sum(case((ToolCallLog.ok.is_(True), 1), else_=0)),
               func.sum(case((ToolCallLog.ok.is_(False), 1), else_=0)))
        .where(ToolCallLog.created_at >= since).group_by(ToolCallLog.tool)
    )).all()
    tool_agg = {t: (int(c), int(o or 0), int(e or 0)) for t, c, o, e in agg_rows}

    spend = await _spend_snapshot(ctx)

    def _cap_for(metric: str) -> dict[str, Any] | None:
        s = spend.get(metric)
        if not s or s.get("cap_per_day") is None:
            return None
        spent, cap = s["spent_today"], s["cap_per_day"]
        if metric == "llm_micros":
            detail = f"${spent/1e6:.2f} / ${cap/1e6:.2f}"
        elif metric == "bq_bytes":
            detail = f"{_fmt_bytes(spent)} / {_fmt_bytes(cap)}"
        else:
            detail = f"{spent:,} / {cap:,} units"
        return {"pct": round(100 * spent / cap, 1) if cap else 0.0, "detail": detail}

    counts = {"healthy": 0, "degraded": 0, "down": 0, "idle": 0}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for category, name, owner, access, uses, check in SYSTEMS_META:
        kind, key = check
        note: str | None = None
        # status ∈ {online, offline, configured, missing}; summary counts treat
        # everything except "missing" as configured (offline = set up but down).
        if kind == "service":
            note = endpoints.get(key)
            status = "missing" if not note else ("online" if reachable.get(key) else "offline")
        elif kind == "cred":
            status = "configured" if present.get(key, False) else "missing"
        elif kind == "env":
            status = "configured" if creds.has(key) else "missing"
        elif kind == "endpoint":
            note = endpoints.get(key)
            status = "configured" if note else "missing"
        else:  # always
            status = "configured"

        calls = ok = err = 0
        for tname in _SYSTEM_TOOLS.get(name, []):
            c, o, e = tool_agg.get(tname, (0, 0, 0))
            calls, ok, err = calls + c, ok + o, err + e
        success = round(100 * ok / calls) if calls else None

        cap = _cap_for(_SYSTEM_METRIC[name]) if name in _SYSTEM_METRIC else None
        cappct = cap["pct"] if cap else None

        # Health: missing config takes precedence (idle); an offline service or a
        # blown cap is down; recent errors or a near-cap read as degraded.
        if status == "offline":
            health = "down"
        elif status == "missing":
            health = "idle"
        elif cappct is not None and cappct >= 100:
            health = "down"
        elif err > 0 or (cappct is not None and cappct >= 80):
            health = "degraded"
        else:
            health = "healthy"
        counts[health] += 1

        grouped.setdefault(category, []).append(
            {"name": name, "category": category, "owner": owner, "access": access, "uses": uses,
             "status": status, "configured": status != "missing", "note": note,
             "domain": _SYSTEM_DOMAINS.get(name), "health": health, "cap": cap,
             "calls_24h": calls, "errors_24h": err, "success_pct": success}
        )
    matrix = [{"category": c, "systems": s} for c, s in grouped.items()]
    return matrix, counts


@router.get("/systems", response_class=HTMLResponse)
async def systems_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    async with RunContext.open() as ctx:
        groups, health = await _system_matrix(ctx)
        total = sum(len(g["systems"]) for g in groups)
        connected = sum(1 for g in groups for s in g["systems"] if s["configured"])
    return templates.TemplateResponse(request, "systems.html",
                                      {"user": user, "groups": groups, "total": total,
                                       "connected": connected, "health": health,
                                       "consolidation": CONSOLIDATION, "decisions": DECISIONS})


@router.get("/distribution", response_class=HTMLResponse)
async def distribution_page(request: Request):
    """Review the assembled outbound artifacts (PRD §6.6 / §2 success scenario).
    Draft + human-send only — nothing distributes autonomously."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    kinds = {"daily_digest_inputs": ("Daily digest", "digest"), "daily_digest": ("Daily digest", "digest"),
             "newsletter_draft": ("CarBuzz newsletter", "newsletter"), "social_draft": ("Social posts", "social")}
    async with RunContext.open() as ctx:
        rows = await ctx.store.query(types=[EntryType.REPORT, EntryType.DISTRIBUTION_DRAFT],
                                     status=None, limit=100)
        items = []
        for r in rows:
            p = r.payload or {}
            kind = p.get("kind", "")
            label, channel = kinds.get(kind, (kind, "other"))
            ref = p.get("artifact_ref") if isinstance(p.get("artifact_ref"), dict) else None
            web_url = None
            if ref:
                web_url = f"/artifacts/{ref['key']}" if ref.get("backend") == "local" and ref.get("key") else ref.get("uri")
            items.append({"id": r.id, "brand": r.brand, "label": label, "channel": channel,
                          "type": r.type.value, "status": p.get("status") or ("ready" if p.get("ready") else "inputs"),
                          "artifact": ref, "artifact_url": web_url,
                          "bytes": ref.get("bytes") if ref else None,
                          "created_at": r.created_at.strftime("%m-%d %H:%M") if r.created_at else ""})
    channels = {"digest": sum(1 for i in items if i["channel"] == "digest"),
                "newsletter": sum(1 for i in items if i["channel"] == "newsletter"),
                "social": sum(1 for i in items if i["channel"] == "social")}
    assembled = sum(1 for i in items if i["status"] in ("assembled", "ready"))
    return templates.TemplateResponse(request, "distribution.html",
                                      {"user": user, "items": items, "channels": channels,
                                       "assembled": assembled})


@router.get("/artifacts/{key:path}")
async def artifact(request: Request, key: str):
    """Serve a locally-stored artifact (read-only, path-traversal-guarded)."""
    require_user(request)
    root = Path(get_settings().artifacts.local_dir).resolve()
    target = (root / key).resolve()
    # is_relative_to, not startswith: a bare prefix also matches sibling dirs
    # that merely share the name (…/local_artifacts_old).
    if not target.is_relative_to(root) or not target.is_file():
        return HTMLResponse("<h3>Artifact not found</h3>", status_code=404)
    return FileResponse(target)


# ---------------------------------------------------------------------------
# Competitor trend pipeline (docs/trend-pipeline.md)
# ---------------------------------------------------------------------------

def _artifact_url(ref: dict[str, Any] | None) -> str | None:
    if not isinstance(ref, dict):
        return None
    if ref.get("backend") == "local" and ref.get("key"):
        return f"/artifacts/{ref['key']}"
    return ref.get("uri")


def _artifact_text(ref: dict[str, Any] | None, max_bytes: int = 12_000) -> str | None:
    """Inline preview of a local artifact (guarded like GET /artifacts)."""
    if not isinstance(ref, dict) or ref.get("backend") != "local" or not ref.get("key"):
        return None
    root = Path(get_settings().artifacts.local_dir).resolve()
    target = (root / ref["key"]).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    try:
        data = target.read_bytes()[:max_bytes]
        text = data.decode("utf-8", errors="replace")
        return text + ("\n\n… (truncated — open the full artifact)" if target.stat().st_size > max_bytes else "")
    except OSError:
        return None


def _fmt_dt(dt: datetime | None, fmt: str = "%m-%d %H:%M") -> str:
    return dt.strftime(fmt) if dt else ""


def _trend_dict(t: Trend, *, with_dossier: bool = False) -> dict[str, Any]:
    pipelines = sorted(t.pipelines or [], key=lambda p: p.id or 0)
    out: dict[str, Any] = {
        "id": t.id, "brand": t.brand, "headline": t.headline, "summary": t.summary,
        "score": round(t.score or 0), "velocity": t.velocity,
        "source_count": t.source_count, "signal_count": t.signal_count,
        "covered_by_us": t.covered_by_us, "status": t.status, "origin": t.origin,
        "oems": (t.entities or {}).get("oems", []),
        "breaking": (t.score_breakdown or {}).get("breaking", 0) > 0,
        "watchlisted": (t.score_breakdown or {}).get("watchlist", 0) > 0,
        "first_seen": _fmt_dt(t.first_seen_at), "last_seen": _fmt_dt(t.last_seen_at),
        "expires_at": _fmt_dt(t.expires_at, "%m-%d %H:%M UTC"),
        "pipelines": [{"id": p.id, "status": p.status} for p in pipelines],
        "pending_pipeline_id": next((p.id for p in pipelines
                                     if p.status == "pending_approval"), None),
        "has_dossier": bool(t.dossier),
    }
    if with_dossier:
        out.update({
            "score_breakdown": {k: round(v, 1) for k, v in (t.score_breakdown or {}).items() if v},
            "evidence": t.evidence or [], "dossier": t.dossier or {},
            "dossier_url": _artifact_url(t.dossier_ref),
        })
    return out


def _pipeline_dict(p: ContentPipeline, *, with_jobs: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": p.id, "brand": p.brand, "status": p.status, "trend_id": p.trend_id,
        "headline": p.trend.headline if p.trend else f"pipeline #{p.id}",
        "trend_score": round(p.trend.score) if p.trend and p.trend.score else None,
        "requested_by": p.requested_by, "approved_by": p.approved_by,
        "approved_at": _fmt_dt(p.approved_at), "declined_by": p.declined_by,
        "close_reason": p.close_reason, "instructions": p.instructions,
        "content_types": p.content_types or [], "created_at": _fmt_dt(p.created_at),
        "events": list(reversed(p.events or []))[:20],
        "open": p.status in PIPELINE_OPEN_STATUSES,
    }
    if with_jobs:
        out["jobs"] = [_job_dict(j) for j in sorted(p.jobs or [], key=lambda j: j.id or 0)]
    else:
        out["job_count"] = len(p.jobs or [])
    return out


def _job_dict(j: ContentJob) -> dict[str, Any]:
    meta = j.preview_meta or {}
    return {
        "id": j.id, "content_type": j.content_type, "transport": j.transport,
        "status": j.status, "attempt": j.attempt, "instructions": j.instructions,
        "error": j.error, "title": meta.get("title") or j.content_type.replace("_", " "),
        "word_count": meta.get("word_count"), "generator": meta.get("generator"),
        "preview_url": _artifact_url(j.preview_ref),
        "preview_text": _artifact_text(j.preview_ref) if j.preview_ref else None,
        "result": j.result_ref, "reviewed_by": j.reviewed_by,
        "history": j.history or [], "cost": j.cost,
        "updated_at": _fmt_dt(j.updated_at or j.created_at),
    }


@router.get("/trends", response_class=HTMLResponse)
async def trends_page(request: Request, brand: str | None = None, scanning: str | None = None):
    """Trend Radar: open trends ranked by score + pending trigger requests."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    settings = get_settings()
    async with RunContext.open() as ctx:
        repo = TrendRepo(ctx.session)
        open_trends = [_trend_dict(t) for t in await repo.list(
            brand=brand or None,
            statuses=["detected", "dossier_building", "proposed", "approved"], limit=60)]
        recent_closed = [_trend_dict(t) for t in await repo.list(
            brand=brand or None,
            statuses=["dismissed", "declined", "expired", "completed"], limit=15)]
        pipelines = [_pipeline_dict(p, with_jobs=False)
                     for p in await PipelineRepo(ctx.session).list(brand=brand or None, limit=40)]
        coverage = await _brand_coverage(ctx, list(settings.brand_keys))
    # Approvability is per row — a brand_user may approve their brand's rows on
    # an unfiltered (portfolio-wide) page.
    for row in (*open_trends, *recent_closed, *pipelines):
        row["can_approve"] = _can_approve(request, row["brand"])
    pending = [p for p in pipelines if p["status"] == "pending_approval"]
    stats = {
        "open": len(open_trends),
        "pending": len(pending),
        "generating": sum(1 for p in pipelines if p["status"] in ("approved", "generating")),
        "previews": sum(1 for p in pipelines if p["status"] == "previews_ready"),
        "gaps": sum(1 for t in open_trends if t["covered_by_us"] is False),
    }
    return templates.TemplateResponse(
        request, "trends.html",
        {"user": user, "trends": open_trends, "closed": recent_closed, "pending": pending,
         "pipelines": pipelines[:20], "stats": stats, "brands": list(settings.brand_keys),
         "content_types": list(CONTENT_TYPES), "coverage": coverage,
         "kill_switch": settings.kill_switch, "trend_cfg": settings.trends,
         "scanning": scanning == "1", "f": {"brand": brand or ""}},
    )


async def _brand_coverage(ctx: RunContext, brand_keys: list[str]) -> list[dict[str, Any]]:
    """Per-brand coverage scorecard from the trend table (Lovable's brandCoverage).
    Score = % of open trends we've covered; no day-delta (no history kept)."""
    settings = get_settings()
    open_st = ["detected", "dossier_building", "proposed", "approved"]
    out = []
    for b in brand_keys:
        async def _c(*conds):
            return int((await ctx.session.execute(
                select(func.count()).select_from(Trend).where(*conds))).scalar_one())
        covered = await _c(Trend.brand == b, Trend.covered_by_us.is_(True), Trend.status.in_(open_st))
        gaps = await _c(Trend.brand == b, Trend.covered_by_us.is_(False), Trend.status.in_(open_st))
        pending = int((await ctx.session.execute(
            select(func.count()).select_from(ContentPipeline)
            .where(ContentPipeline.brand == b, ContentPipeline.status == "pending_approval")
        )).scalar_one())
        denom = covered + gaps
        try:
            display = settings.brand(b).display_name
        except KeyError:
            display = b
        out.append({"brand": b, "display_name": display,
                    "score": round(100 * covered / denom) if denom else None,
                    "covered": covered, "gaps": gaps, "pending": pending})
    return out


@router.post("/trends/scan")
async def trends_scan(request: Request, background_tasks: BackgroundTasks,
                      brand: str = Form("portfolio")):
    """Kick a scan in the background (sources + LLM dossiers can take a minute).
    Scans hit paid source APIs + build LLM dossiers, so viewers can't trigger them."""
    user = require_user(request)
    brand = brand or "portfolio"
    if not get_settings().is_valid_scope(brand):
        return JSONResponse({"error": f"unknown brand '{brand}'"}, status_code=400)
    if not can_approve(user.get("role", "viewer"), user.get("brands"), brand):
        return _FORBIDDEN
    from ..trends.scout import run_trend_scan

    background_tasks.add_task(run_trend_scan, brand)
    return RedirectResponse("/trends?scanning=1", status_code=302)


@router.post("/trends/manual")
async def trends_manual(request: Request, background_tasks: BackgroundTasks,
                        topic: str = Form(...), brand: str = Form("portfolio"),
                        url: str = Form("")):
    """Editor-pasted trend — rides the same dossier/trigger/approval path."""
    user = require_user(request)
    if not topic.strip():
        return JSONResponse({"error": "topic is required"}, status_code=400)
    from ..trends.scout import add_manual_trend

    async with RunContext.open() as ctx:
        if not ctx.settings.is_valid_scope(brand):
            return JSONResponse({"error": f"unknown brand '{brand}'"}, status_code=400)
        try:
            trend = await add_manual_trend(ctx, topic=topic.strip(), brand=brand,
                                           actor=user["email"], url=url.strip() or None)
            trend_id = trend.id
        except Exception as exc:  # noqa: BLE001 — surfaced to the editor, not a 500
            log.warning("manual trend failed: %s", exc)
            return JSONResponse({"error": f"could not create trend: {exc}"}, status_code=400)
    return RedirectResponse(f"/trends/{trend_id}", status_code=302)


@router.get("/trends/{trend_id}", response_class=HTMLResponse)
async def trend_detail(request: Request, trend_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    settings = get_settings()
    async with RunContext.open() as ctx:
        trend = await TrendRepo(ctx.session).get(trend_id)
        if trend is None:
            return HTMLResponse("<h3>Trend not found</h3>", status_code=404)
        data = _trend_dict(trend, with_dossier=True)
        pipelines = [_pipeline_dict(p) for p in
                     sorted(trend.pipelines or [], key=lambda p: -(p.id or 0))]
        # Fact-gate state for this trend: verified facts vs pending claims.
        claims = await ctx.store.query(
            brand=trend.brand, types=[EntryType.CLAIM],
            payload_contains={"kind": "trend_key_fact", "trend_id": trend.id},
            status=None, limit=20)
        facts = await ctx.store.query(
            brand=trend.brand, types=[EntryType.FACT], verified=True,
            payload_contains={"kind": "verified_fact"}, limit=50)
        statements = {(c.payload or {}).get("statement") for c in claims}
        verified = [f.payload.get("statement") for f in facts
                    if f.payload and f.payload.get("statement") in statements]
        pending_claims = [{"statement": (c.payload or {}).get("statement"), "status": c.status}
                          for c in claims
                          if (c.payload or {}).get("statement") not in set(verified)]
    may_approve = _can_approve(request, data["brand"])
    return templates.TemplateResponse(
        request, "trend_detail.html",
        {"user": user, "t": data, "pipelines": pipelines,
         "verified_facts": verified, "claims": pending_claims,
         "content_types": list(CONTENT_TYPES),
         "default_types": list(settings.trends.default_content_types),
         "can_approve": may_approve, "kill_switch": settings.kill_switch},
    )


@router.post("/trends/{trend_id}/dismiss")
async def trend_dismiss(request: Request, trend_id: int):
    user = require_user(request)
    async with RunContext.open() as ctx:
        repo = TrendRepo(ctx.session)
        trend = await repo.get(trend_id)
        if trend is None:
            return JSONResponse({"error": "trend not found"}, status_code=404)
        if not _can_approve(request, trend.brand):
            return _FORBIDDEN
        try:
            await repo.dismiss(trend_id, user["email"])
        except LifecycleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return RedirectResponse("/trends", status_code=302)


@router.post("/trends/{trend_id}/trigger")
async def trend_trigger(request: Request, background_tasks: BackgroundTasks, trend_id: int,
                        instructions: str = Form(""), approve_now: str = Form("")):
    """Create a trigger request for a trend. Approvers can create-and-approve in
    one step; everyone else creates a pending request for an approver."""
    user = require_user(request)
    form = await request.form()
    picked = [str(v) for v in form.getlist("content_types")]
    wants_approve = approve_now.lower() in ("1", "true", "on", "yes")
    async with RunContext.open() as ctx:
        trend = await TrendRepo(ctx.session).get(trend_id)
        if trend is None:
            return JSONResponse({"error": "trend not found"}, status_code=404)
        # Check-then-mutate: nothing is created if the request will be refused.
        if wants_approve and not _can_approve(request, trend.brand):
            return _FORBIDDEN
        if trend.status not in ("detected", "dossier_building", "proposed"):
            return JSONResponse(
                {"error": f"trend is {trend.status} — it can no longer be triggered"},
                status_code=400)
        repo = PipelineRepo(ctx.session)
        try:
            pipeline = await repo.create(
                trend_id=trend.id, brand=trend.brand,
                content_types=picked or list(get_settings().trends.default_content_types),
                requested_by=user["email"], instructions=instructions.strip() or None)
            trend.status = "proposed" if trend.status in ("detected", "dossier_building") else trend.status
            pipeline_id = pipeline.id
            if wants_approve:
                await approve_and_start(ctx, pipeline_id, user["email"])
        except LifecycleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    background_tasks.add_task(run_job_sweep)
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=302)


@router.get("/pipelines", response_class=HTMLResponse)
async def pipelines_page(request: Request, brand: str | None = None, status: str | None = None):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    settings = get_settings()
    async with RunContext.open() as ctx:
        rows = await PipelineRepo(ctx.session).list(
            brand=brand or None, statuses=[status] if status else None, limit=80)
        pipelines = [_pipeline_dict(p, with_jobs=False) for p in rows]
    counts: dict[str, int] = {}
    for p in pipelines:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    return templates.TemplateResponse(
        request, "pipelines.html",
        {"user": user, "pipelines": pipelines, "counts": counts,
         "brands": list(settings.brand_keys), "kill_switch": settings.kill_switch,
         "f": {"brand": brand or "", "status": status or ""}},
    )


@router.get("/pipelines/{pipeline_id}", response_class=HTMLResponse)
async def pipeline_detail(request: Request, pipeline_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    async with RunContext.open() as ctx:
        pipeline = await PipelineRepo(ctx.session).get(pipeline_id)
        if pipeline is None:
            return HTMLResponse("<h3>Pipeline not found</h3>", status_code=404)
        data = _pipeline_dict(pipeline)
        trend = _trend_dict(pipeline.trend) if pipeline.trend else None
    may_approve = _can_approve(request, data["brand"])
    settings = get_settings()
    return templates.TemplateResponse(
        request, "pipeline_detail.html",
        {"user": user, "p": data, "trend": trend, "can_approve": may_approve,
         "content_types": list(CONTENT_TYPES), "kill_switch": settings.kill_switch},
    )


@router.post("/pipelines/{pipeline_id}/approve")
async def pipeline_approve(request: Request, background_tasks: BackgroundTasks,
                           pipeline_id: int, instructions: str = Form("")):
    user = require_user(request)
    form = await request.form()
    picked = [str(v) for v in form.getlist("content_types")]
    async with RunContext.open() as ctx:
        pipeline = await PipelineRepo(ctx.session).get(pipeline_id)
        if pipeline is None:
            return JSONResponse({"error": "pipeline not found"}, status_code=404)
        if not _can_approve(request, pipeline.brand):
            return _FORBIDDEN
        try:
            await approve_and_start(ctx, pipeline_id, user["email"],
                                    content_types=picked or None,
                                    instructions=instructions.strip() or None)
        except LifecycleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    background_tasks.add_task(run_job_sweep)
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=302)


@router.post("/pipelines/{pipeline_id}/decline")
async def pipeline_decline(request: Request, pipeline_id: int, reason: str = Form("")):
    user = require_user(request)
    async with RunContext.open() as ctx:
        pipeline = await PipelineRepo(ctx.session).get(pipeline_id)
        if pipeline is None:
            return JSONResponse({"error": "pipeline not found"}, status_code=404)
        if not _can_approve(request, pipeline.brand):
            return _FORBIDDEN
        try:
            await decline_pipeline(ctx, pipeline_id, user["email"], reason.strip() or None)
        except LifecycleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return RedirectResponse("/trends", status_code=302)


@router.post("/pipelines/{pipeline_id}/close")
async def pipeline_close(request: Request, pipeline_id: int, reason: str = Form("")):
    user = require_user(request)
    async with RunContext.open() as ctx:
        repo = PipelineRepo(ctx.session)
        pipeline = await repo.get(pipeline_id)
        if pipeline is None:
            return JSONResponse({"error": "pipeline not found"}, status_code=404)
        if not _can_approve(request, pipeline.brand):
            return _FORBIDDEN
        try:
            await repo.close(pipeline_id, user["email"], reason.strip() or None)
        except LifecycleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=302)


@router.post("/jobs/{job_id}/review")
async def job_review(request: Request, job_id: int, verdict: str = Form(...)):
    """Editor verdict on a preview: 'approve' or 'reject'. RBAC runs against the
    job's OWN pipeline — never a caller-supplied id."""
    user = require_user(request)
    async with RunContext.open() as ctx:
        repo = PipelineRepo(ctx.session)
        job = await repo.get_job(job_id)
        if job is None or job.pipeline is None:
            return JSONResponse({"error": "job not found"}, status_code=404)
        if not _can_approve(request, job.pipeline.brand):
            return _FORBIDDEN
        pipeline_id = job.pipeline_id
        try:
            await repo.review_job(job_id, user["email"], approve=verdict == "approve")
            await repo.refresh_rollup(pipeline_id)
        except LifecycleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=302)


@router.post("/jobs/{job_id}/regenerate")
async def job_regenerate(request: Request, background_tasks: BackgroundTasks, job_id: int,
                         instructions: str = Form("")):
    user = require_user(request)
    if not instructions.strip():
        return JSONResponse({"error": "tell the generator what to change"}, status_code=400)
    async with RunContext.open() as ctx:
        repo = PipelineRepo(ctx.session)
        job = await repo.get_job(job_id)
        if job is None or job.pipeline is None:
            return JSONResponse({"error": "job not found"}, status_code=404)
        if not _can_approve(request, job.pipeline.brand):
            return _FORBIDDEN
        pipeline_id = job.pipeline_id
        try:
            await repo.regenerate_job(job_id, user["email"], instructions)
            await repo.refresh_rollup(pipeline_id)
        except LifecycleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    background_tasks.add_task(run_job_sweep)
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=302)


@router.post("/jobs/{job_id}/publish")
async def job_publish(request: Request, job_id: int):
    """The second human gate: Emaki unpublished-draft push (hc_viral transport)
    or an explicit manual hand-off. Confirmed in the UI; refused on kill switch."""
    user = require_user(request)
    async with RunContext.open() as ctx:
        repo = PipelineRepo(ctx.session)
        job = await repo.get_job(job_id)
        if job is None or job.pipeline is None:
            return JSONResponse({"error": "job not found"}, status_code=404)
        if not _can_approve(request, job.pipeline.brand):
            return _FORBIDDEN
        pipeline_id = job.pipeline_id
        try:
            await publish_job(ctx, job_id, user["email"])
        except LifecycleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001 — external CMS failure, readable not 500
            log.warning("publish failed for job %s: %s", job_id, exc)
            return JSONResponse({"error": f"publish failed: {exc}"}, status_code=502)
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=302)


@router.get("/governor", response_class=HTMLResponse)
async def governor_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    settings = get_settings()
    caps = settings.caps
    async with RunContext.open() as ctx:
        spend = await _spend_snapshot(ctx)
        flags = await ctx.store.query(source_agent="governor", types=[EntryType.FLAG], status=None, limit=15)
        cap_flags = [{"metric": (f.payload or {}).get("metric"), "scope": (f.payload or {}).get("scope"),
                      "would_be": (f.payload or {}).get("would_be"), "cap": (f.payload or {}).get("cap"),
                      "action": (f.payload or {}).get("action_type"),
                      "created_at": f.created_at.strftime("%m-%d %H:%M") if f.created_at else ""} for f in flags]
    cap_rows = [
        {"metric": m, "per_day": caps.per_day(m), "per_run": caps.per_run(m),
         "spent": spend[m]["spent_today"], "pct": spend[m]["pct"]}
        for m in ("ahrefs_units", "llm_micros", "bq_bytes")
    ]
    return templates.TemplateResponse(request, "governor.html",
                                      {"user": user, "kill_switch": settings.kill_switch,
                                       "dry_run_default": settings.dry_run_default, "cap_rows": cap_rows,
                                       "cap_flags": cap_flags, "secrets_backend": settings.creds.secrets_backend,
                                       "caps_enabled": caps.enabled})


@router.get("/observability", response_class=HTMLResponse)
async def observability(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    settings = get_settings()
    async with RunContext.open() as ctx:
        spend = await _spend_snapshot(ctx)
        audit = await _recent_audit(ctx, 40)
        mem_counts = await _memory_counts(ctx)
        agents = await _agent_breakdown(ctx)
        trend = await _spend_trend(ctx)
    tot_calls = sum(a["calls"] for a in agents)
    tot_ok = sum(a["ok"] for a in agents)
    totals = {"calls": tot_calls, "acts": sum(a["acts"] for a in agents),
              "ok_rate": round(100 * tot_ok / tot_calls) if tot_calls else None,
              "llm_usd": round(spend["llm_micros"]["spent_today"] / 1e6, 2),
              "bq_mb": round(spend["bq_bytes"]["spent_today"] / 1048576, 1)}
    return templates.TemplateResponse(
        request, "observability.html",
        {"user": user, "spend": spend, "audit": audit, "mem_counts": mem_counts,
         "caps": settings.caps, "agents": agents, "charts": _spend_charts(trend), "totals": totals},
    )


@router.get("/api/spend")
async def api_spend(request: Request):
    require_user(request)
    async with RunContext.open() as ctx:
        return JSONResponse(await _spend_snapshot(ctx))


@router.get("/api/memory")
async def api_memory(request: Request, brand: str | None = None, type: str | None = None,
                     verified: bool | None = None, limit: int = 100, format: str = "json"):
    require_user(request)
    types = [EntryType(type)] if type else None
    async with RunContext.open() as ctx:
        rows = await ctx.store.query(brand=brand, types=types, verified=verified, status=None, limit=limit)
        out = [{"id": r.id, "type": r.type.value, "brand": r.brand, "source_agent": r.source_agent,
                "source_system": r.source_system, "verified": r.verified, "confidence": r.confidence,
                "source_urls": r.source_urls, "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "payload": r.payload} for r in rows]
    if format == "csv":
        cols = ["id", "type", "brand", "source_agent", "source_system", "verified",
                "confidence", "status", "created_at"]
        return _csv_response(out, cols, "memory.csv")
    return JSONResponse({"count": len(out), "entries": out})


@router.get("/api/audit")
async def api_audit(request: Request, limit: int = 100):
    require_user(request)
    async with RunContext.open() as ctx:
        return JSONResponse({"calls": await _recent_audit(ctx, limit)})


# -- helpers ----------------------------------------------------------------

async def _spend_snapshot(ctx: RunContext) -> dict[str, Any]:
    gov = ctx.governor
    caps = ctx.settings.caps
    out = {}
    for metric in ("ahrefs_units", "llm_micros", "bq_bytes"):
        spent = await gov.spent_today(metric)
        cap = caps.per_day(metric)
        out[metric] = {"spent_today": spent, "cap_per_day": cap,
                       "remaining": (cap - spent) if cap is not None else None,
                       "pct": round(100 * spent / cap, 1) if cap else None}
    out["kill_switch"] = ctx.settings.kill_switch
    return out


async def _spend_deltas(ctx: RunContext, spend: dict[str, Any]) -> dict[str, Any]:
    """Today-vs-yesterday % change per metric, from spend_ledger — real numbers
    for the dashboard KPI delta chips. pct is None when there's no baseline."""
    from datetime import date, timedelta

    from ..db.models import SpendLedger

    yesterday = date.today() - timedelta(days=1)
    rows = (await ctx.session.execute(
        select(SpendLedger.metric, func.sum(SpendLedger.amount))
        .where(SpendLedger.window_date == yesterday).group_by(SpendLedger.metric)
    )).all()
    prev = {m: int(a or 0) for m, a in rows}
    out: dict[str, Any] = {}
    for metric in ("llm_micros", "bq_bytes", "ahrefs_units"):
        today = spend.get(metric, {}).get("spent_today", 0)
        y = prev.get(metric, 0)
        out[metric] = {"prev": y, "pct": round(100 * (today - y) / y, 1) if y else None}
    return out


# Trend-source APIs surfaced on the dashboard, keyed by the exact tool_call_log
# names each adapter/dossier step logs (BaseAdapter.observe logs tool=<adapter
# name>; the dossier logs tavily_deep_search). Firecrawl/NewsAPI/Perplexity are
# still called + audited, just not shown in this curated 7-row panel.
_API_RESOURCES = [
    ("Tavily", "tavily.com", ["tavily_trends", "tavily_deep_search"]),
    ("YouTube", "youtube.com", ["youtube_trends"]),
    ("X", "x.com", ["x_trends"]),
    ("Semrush", "semrush.com", ["semrush_trends"]),
]


async def _resource_usage(ctx: RunContext, spend: dict[str, Any],
                          deltas: dict[str, Any]) -> list[dict[str, Any]]:
    """Resource-usage rows for the dashboard — all backed by real tracking:
    LLM/BigQuery/Ahrefs from the spend ledger, and the trend-source APIs counted
    from the tool_call_log audit trail (today vs yesterday). Each carries a
    `domain` used to render the service's real logo."""
    from datetime import datetime as _dt, timedelta, timezone as _tz

    rows: list[dict[str, Any]] = [
        {"label": "LLM spend", "domain": "anthropic.com",
         "val": f"${spend['llm_micros']['spent_today']/1e6:.2f}",
         "prev": f"${deltas['llm_micros']['prev']/1e6:.2f}", "pct": deltas["llm_micros"]["pct"]},
        {"label": "BigQuery", "domain": "cloud.google.com",
         "val": f"{spend['bq_bytes']['spent_today']/1048576:.0f} MB",
         "prev": f"{deltas['bq_bytes']['prev']/1048576:.0f} MB", "pct": deltas["bq_bytes"]["pct"]},
        {"label": "Ahrefs units", "domain": "ahrefs.com",
         "val": f"{spend['ahrefs_units']['spent_today']:,}",
         "prev": f"{deltas['ahrefs_units']['prev']:,}", "pct": deltas["ahrefs_units"]["pct"]},
    ]
    now = _dt.now(_tz.utc)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yest0 = today0 - timedelta(days=1)
    for label, domain, tools in _API_RESOURCES:
        today_n = int((await ctx.session.execute(
            select(func.count()).select_from(ToolCallLog)
            .where(ToolCallLog.tool.in_(tools), ToolCallLog.created_at >= today0)
        )).scalar_one())
        prev_n = int((await ctx.session.execute(
            select(func.count()).select_from(ToolCallLog)
            .where(ToolCallLog.tool.in_(tools),
                   ToolCallLog.created_at >= yest0, ToolCallLog.created_at < today0)
        )).scalar_one())
        rows.append({"label": f"{label} calls", "domain": domain,
                     "val": f"{today_n:,}", "prev": f"{prev_n:,}",
                     "pct": round(100 * (today_n - prev_n) / prev_n, 1) if prev_n else None})
    return rows


async def _recent_audit(ctx: RunContext, limit: int) -> list[dict[str, Any]]:
    rows = (await ctx.session.execute(
        select(ToolCallLog).order_by(desc(ToolCallLog.created_at)).limit(limit)
    )).scalars().all()
    return [{"id": r.id, "agent": r.agent, "tool": r.tool, "action": r.action, "brand": r.brand,
             "dry_run": r.dry_run, "ok": r.ok, "cost": r.cost,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]


async def _memory_counts(ctx: RunContext) -> dict[str, int]:
    rows = (await ctx.session.execute(
        select(MemoryEntry.type, func.count()).where(MemoryEntry.status == "active").group_by(MemoryEntry.type)
    )).all()
    return {t.value if hasattr(t, "value") else str(t): int(c) for t, c in rows}


async def _portfolio_summary(ctx: RunContext, brands: list[str]) -> dict[str, Any]:
    """Cross-brand at-a-glance: active flags + latest plan status per brand."""
    settings = get_settings()
    out = []
    for b in brands:
        flag_ct = int((await ctx.session.execute(
            select(func.count()).where(MemoryEntry.brand == b, MemoryEntry.type == EntryType.FLAG,
                                       MemoryEntry.status == "active")
        )).scalar_one())
        latest = await PlanRepo(ctx.session).latest_plan(b)
        out.append({"brand": b, "display_name": settings.brand(b).display_name,
                    "active_flags": flag_ct,
                    "latest_plan": latest.id if latest else None,
                    "latest_status": latest.status if latest else "—"})
    return {"brands": out, "spend": await _spend_snapshot(ctx)}


async def _home_stats(ctx: RunContext, plans: list[dict[str, Any]]) -> dict[str, Any]:
    flags = int((await ctx.session.execute(
        select(func.count()).where(MemoryEntry.type == EntryType.FLAG, MemoryEntry.status == "active")
    )).scalar_one())
    facts = int((await ctx.session.execute(
        select(func.count()).where(MemoryEntry.type == EntryType.FACT, MemoryEntry.verified.is_(True),
                                   MemoryEntry.status == "active")
    )).scalar_one())
    today = date.today().isoformat()
    spend = await _spend_snapshot(ctx)
    return {
        "active_flags": flags,
        "verified_facts": facts,
        "plans_today": sum(1 for p in plans if p["plan_date"] == today),
        "llm_usd": round(spend["llm_micros"]["spent_today"] / 1_000_000, 2),
        "bq_mb": round(spend["bq_bytes"]["spent_today"] / 1_048_576, 1),
    }


async def _agent_breakdown(ctx: RunContext) -> list[dict[str, Any]]:
    """Per-agent tool-call activity from the audit log."""
    rows = (await ctx.session.execute(
        select(ToolCallLog.agent, func.count(),
               func.sum(case((ToolCallLog.ok.is_(True), 1), else_=0)),
               func.sum(case((ToolCallLog.action == "act", 1), else_=0)))
        .group_by(ToolCallLog.agent).order_by(func.count().desc())
    )).all()
    return [{"agent": a, "calls": int(c), "ok": int(ok or 0), "acts": int(acts or 0)}
            for a, c, ok, acts in rows]


async def _spend_trend(ctx: RunContext) -> list[dict[str, Any]]:
    """Daily spend by metric over the recent window (from spend_ledger)."""
    from ..db.models import SpendLedger

    rows = (await ctx.session.execute(
        select(SpendLedger.window_date, SpendLedger.metric, func.sum(SpendLedger.amount))
        .group_by(SpendLedger.window_date, SpendLedger.metric)
        .order_by(SpendLedger.window_date.desc()).limit(30)
    )).all()
    by_date: dict[str, dict[str, int]] = {}
    for d, metric, amt in rows:
        by_date.setdefault(d.isoformat(), {})[metric] = int(amt)
    return [{"date": d, **v} for d, v in sorted(by_date.items(), reverse=True)][:7]


def _spend_charts(trend: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Small-multiple bar-chart geometry, one per metric (each its own scale —
    never a shared axis). Returns SVG-ready bars oldest→newest."""
    days = list(reversed(trend))  # oldest → newest
    metrics = [
        ("llm_micros", "LLM spend", lambda v: f"${v/1e6:.2f}"),
        ("bq_bytes", "BigQuery scanned", lambda v: f"{v/1048576:.0f} MB"),
        ("ahrefs_units", "Ahrefs units", lambda v: f"{int(v)}"),
    ]
    bw, gap, pad, maxh, top = 22, 8, 4, 48, 8
    charts = []
    for key, title, fmt in metrics:
        vals = [int(d.get(key, 0) or 0) for d in days]
        mx = max(vals, default=0) or 1  # default=0 -> no crash when the ledger is empty
        bars = []
        for i, v in enumerate(vals):
            h = round(v / mx * maxh) if v else 0
            bars.append({"x": pad + i * (bw + gap), "y": top + (maxh - h), "w": bw, "h": h,
                         "date": days[i]["date"][5:], "val": fmt(v), "raw": v})
        width = pad * 2 + max(1, len(vals)) * (bw + gap)
        charts.append({"key": key, "title": title, "bars": bars, "width": width,
                       "height": top + maxh + 18, "baseline": top + maxh,
                       "latest": fmt(vals[-1]) if vals else fmt(0),
                       "empty": not any(vals)})
    return charts


def _csv_response(rows: list[dict[str, Any]], cols: list[str], filename: str) -> PlainTextResponse:
    import csv
    import io

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# Activity feed + Notifications (real data: tool_call_log, memory flags, jobs)
# ---------------------------------------------------------------------------

def _ago(dt: datetime | None) -> str:
    if dt is None:
        return ""
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)
    d = dt if dt.tzinfo else dt.replace(tzinfo=_tz.utc)
    secs = max(0, int((now - d).total_seconds()))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


async def _activity_events(ctx: RunContext, limit: int = 60) -> list[dict[str, Any]]:
    """Unified chronological event feed from the audit log + shared-memory writes."""
    events: list[dict[str, Any]] = []
    tcalls = (await ctx.session.execute(
        select(ToolCallLog).order_by(desc(ToolCallLog.created_at)).limit(limit)
    )).scalars().all()
    for r in tcalls:
        act = r.action == "act"
        err = r.ok is False
        events.append({
            "ts": r.created_at, "kind": "error" if err else ("dispatch" if act else "tool_call"),
            "agent": r.agent, "system": r.tool, "brand": r.brand,
            "severity": "bad" if err else ("good" if act else "info"),
            "message": f"{r.agent} {'ran' if act else 'called'} {r.tool}"
                       + ("" if r.ok is None else (" — ok" if r.ok else " — failed"))
                       + ("" if r.dry_run or not act else " (LIVE)"),
        })
    entries = await ctx.store.query(status=None, limit=limit)
    for e in entries:
        typ = e.type.value
        kind = (e.payload or {}).get("kind", "")
        is_gov = typ == "decision" and str(kind).startswith("spend")
        events.append({
            "ts": e.created_at, "kind": "governor" if is_gov else "memory_write",
            "agent": e.source_agent, "system": e.source_system, "brand": e.brand,
            "severity": "warn" if typ == "flag" else ("good" if e.verified else "info"),
            "message": f"{e.source_agent} wrote {typ}" + (f" · {kind}" if kind else ""),
        })
    events.sort(key=lambda x: x["ts"] or datetime.min.replace(tzinfo=None), reverse=True)
    events = events[:limit]
    for ev in events:
        ev["ago"] = _ago(ev["ts"])
        ev["when"] = ev["ts"].strftime("%m-%d %H:%M:%S") if ev["ts"] else ""
        ev.pop("ts", None)
    return events


async def _notification_items(ctx: RunContext, limit: int = 60) -> list[dict[str, Any]]:
    """Human-actionable items: active flags, failed content jobs, failed plan items."""
    out: list[dict[str, Any]] = []
    flags = await ctx.store.query(types=[EntryType.FLAG], status="active", limit=limit)
    for f in flags:
        p = f.payload or {}
        sev = {"high": "bad", "medium": "warn"}.get(str(p.get("severity", "")), "info")
        out.append({"ts": f.created_at, "severity": sev,
                    "title": str(p.get("kind", "flag")).replace("_", " ").title(),
                    "detail": p.get("headline") or p.get("url") or p.get("note")
                              or f"{f.source_agent} · {f.brand}", "brand": f.brand})
    failed_jobs = (await ctx.session.execute(
        select(ContentJob).where(ContentJob.status == "failed")
        .order_by(desc(ContentJob.created_at)).limit(limit)
    )).scalars().all()
    for j in failed_jobs:
        out.append({"ts": j.updated_at or j.created_at, "severity": "bad",
                    "title": f"Content job failed — {j.content_type.replace('_', ' ')}",
                    "detail": (j.error or "generation failed")[:160],
                    "brand": j.pipeline.brand if j.pipeline else None})
    failed_items = (await ctx.session.execute(
        select(PlanItem).where(PlanItem.status == "failed")
        .order_by(desc(PlanItem.created_at)).limit(limit)
    )).scalars().all()
    for it in failed_items:
        ref = it.result_ref or {}
        out.append({"ts": it.created_at, "severity": "bad",
                    "title": f"Plan item failed — {it.action_type}",
                    "detail": str(ref.get("refused") or ref.get("error") or "dispatch failed")[:160],
                    "brand": None})
    out.sort(key=lambda x: x["ts"] or datetime.min.replace(tzinfo=None), reverse=True)
    out = out[:limit]
    for n in out:
        n["ago"] = _ago(n["ts"])
        n.pop("ts", None)
    return out


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    async with RunContext.open() as ctx:
        events = await _activity_events(ctx, 80)
    settings = get_settings()
    return templates.TemplateResponse(request, "activity.html",
                                      {"user": user, "events": events,
                                       "kill_switch": settings.kill_switch})


@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    async with RunContext.open() as ctx:
        items = await _notification_items(ctx, 60)
    settings = get_settings()
    unread = sum(1 for n in items if n["severity"] in ("bad", "warn"))
    return templates.TemplateResponse(request, "notifications.html",
                                      {"user": user, "items": items, "unread": unread,
                                       "kill_switch": settings.kill_switch})


@router.get("/api/nav-badges")
async def nav_badges(request: Request):
    """Sidebar badge counts (actionable notifications). Auth-gated; 0 when signed out."""
    if not current_user(request):
        return JSONResponse({"notifications": 0})
    async with RunContext.open() as ctx:
        items = await _notification_items(ctx, 60)
    return JSONResponse({"notifications": sum(1 for n in items if n["severity"] in ("bad", "warn"))})
