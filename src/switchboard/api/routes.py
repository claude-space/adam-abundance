"""Approval surface + observability routes.

The approval surface (PRD §9) shows the daily plan with ranked items — each with
action, params, rationale, and cost estimate — and lets a human approve / edit /
reject per item, approve the plan, then dispatch. Observability (PRD §12 Phase 5)
exposes spend-vs-caps, a memory browser, and the tool-call audit.
"""

from __future__ import annotations

import hmac
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
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
from ..db.models import MemoryEntry, Plan, PlanItem, ToolCallLog
from ..logging_ import get_logger
from ..orchestrator.dispatch import Dispatcher
from ..orchestrator.plans import ApprovalError, PlanRepo
from ..rbac import ROLE_LABELS, Role, can_approve, can_manage_users, is_valid_role
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
    if brand:
        plans = [p for p in plans if p.brand == brand]
    scope = settings.brand(brand).display_name if brand else "the Auto portfolio"
    latest = plans[0] if plans else None
    lines = [
        f"Switchboard status for {scope}:",
        f"- Shared memory: {by_type.get('fact', 0)} verified facts, "
        f"{by_type.get('flag', 0)} active flags, {by_type.get('claim', 0)} unverified claims.",
        f"- Plans on record: {len(plans)}" + (
            f"; latest ({latest.plan_date.isoformat()}) is '{latest.status}' "
            f"with {len(latest.items)} items." if latest else "."),
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
         "hero": hero, "gauges": gauges, "fleet_max": fleet_max, "caps_enabled": caps.enabled},
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
                    "orchestrator", "decay_scan", "content_audit", "governor", "system"],
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
                     "system": "#6f7887"})
# Entry-type identity colors (memory browser, donuts, badges).
TYPE_COLORS = {"metric": "#5b9dff", "flag": "#d6a021", "fact": "#3fb950", "claim": "#e8833a",
               "decision": "#7c5cff", "context": "#6f7887", "report": "#ec6cb0",
               "distribution_draft": "#26c6a6", "plan_item": "#8ea1b8"}
# Make the color lookups available to every template.
templates.env.globals["agent_color"] = lambda k: AGENT_COLORS.get(k, "#6f7887")
templates.env.globals["type_color"] = lambda t: TYPE_COLORS.get(t, "#6f7887")

FEEDER_META = [
    {"key": "decay_scan", "display": "Ranking-decay scan",
     "domain": "Seona's scanner drops decay candidates into memory on its schedule."},
    {"key": "content_audit", "display": "Content-depth auditor",
     "domain": "Flags thin / low-depth articles into memory on its schedule."},
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
    out = []
    for m in AGENT_META:
        k = m["key"]
        actions = sorted(at for at, cls in ACTION_REGISTRY.items() if cls.owner_agent == k)
        if k == "orchestrator":
            actions = ["notify", *actions]
        c, last = calls.get(k, (0, None))
        out.append({**m, "owns": owned_tool_names(k), "actions": actions, "read_only": not actions,
                    "entries": mem.get(k, 0), "calls": c,
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
#   endpoint -> a configured base URL (shown, not probed)   always -> built-in
SYSTEMS_META: list[dict[str, Any]] = [
    # Warehouse & data
    ("Warehouse & data", "BigQuery", "analytics", "read", "PubInsights consum + ODS article analysis", ("cred", "google_sa_inline")),
    ("Warehouse & data", "Sentinel Pro", "analytics", "read", "Day-of sessions/engagement + conversion events", ("cred", "sentinel")),
    ("Warehouse & data", "Google Sheets", "analytics", "read", "Writer quotas + paid-media RAW_DATA log", ("cred", "google_sa_inline")),
    ("Warehouse & data", "Ahrefs", "opportunity", "read · metered", "Competitor keywords, SERP, backlinks", ("cred", "ahrefs")),
    ("Warehouse & data", "GSC exports", "opportunity", "read", "Search demand (empty for Auto trio — §13.13)", ("cred", "google_sa_inline")),
    ("Warehouse & data", "Similarweb", "research", "read", "Competitor traffic estimates", ("cred", "similarweb")),
    # Editorial pipelines
    ("Editorial pipelines", "Claude Albert", "production", "read + action", "Discover ideation + AI writer + outline reviewer", ("endpoint", "albert")),
    ("Editorial pipelines", "Seona", "opportunity", "read + action", "SEO ideation + ranking-decay + Update Strategist", ("endpoint", "seona")),
    ("Editorial pipelines", "HC Viral Hits", "production", "read + action", "Viral ideation + AI writer + Emaki CMS push", ("env", "HC_VIRAL_HITS_API_KEY")),
    ("Editorial pipelines", "Asana", "production", "read + action", "Tasks + outline-approval workflow", ("cred", "asana")),
    ("Editorial pipelines", "Emaki CMS", "production", "action · gated", "Push unpublished article drafts (via HC-Viral)", ("env", "HC_VIRAL_HITS_API_KEY")),
    ("Editorial pipelines", "content-depth-auditor", "analytics", "feeder", "Content-depth findings → memory", ("env", "CONTENT_AUDITOR_URL")),
    ("Editorial pipelines", "writers-dashboard", "analytics", "read", "Writer-performance metric logic (superseded)", ("endpoint", "writers_dashboard")),
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

CONSOLIDATION = [  # PRD §15 — surfaced, not assumed
    "Two BigQuery article tables (ODS vs consum) feed different systems for the same brands — pick a canonical source for Analytics or map between them.",
    "Two ideation + AI-writer pipelines (Claude Albert / HC Viral Hits) draft in parallel with overlapping brands — memory can de-dup topic angles across both.",
    "Two performance-digest paths (writers-dashboard Slack vs daily-reporting email) compute overlapping per-brand performance daily — share one metric layer.",
    "Two cost-tracking schemes (Albert cost_micros vs HC-Viral compute_cost_cents) — the governor's spend_ledger should absorb both, not add a third.",
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


async def _systems_overview(ctx: RunContext) -> list[dict[str, Any]]:
    creds = ctx.creds
    present = creds.describe()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for category, name, owner, access, uses, check in SYSTEMS_META:
        kind, key = check
        if kind == "cred":
            configured, note = present.get(key, False), None
        elif kind == "env":
            configured, note = creds.has(key), None
        elif kind == "endpoint":
            configured, note = True, ctx.settings.endpoints.get(key)
        else:
            configured, note = True, None
        grouped.setdefault(category, []).append(
            {"name": name, "owner": owner, "access": access, "uses": uses,
             "configured": configured, "note": note, "probed": kind != "endpoint"}
        )
    return [{"category": c, "systems": s} for c, s in grouped.items()]


@router.get("/systems", response_class=HTMLResponse)
async def systems_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    async with RunContext.open() as ctx:
        groups = await _systems_overview(ctx)
        total = sum(len(g["systems"]) for g in groups)
        connected = sum(1 for g in groups for s in g["systems"] if s["configured"])
    return templates.TemplateResponse(request, "systems.html",
                                      {"user": user, "groups": groups, "total": total,
                                       "connected": connected, "consolidation": CONSOLIDATION,
                                       "decisions": DECISIONS})


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
    if not str(target).startswith(str(root)) or not target.is_file():
        return HTMLResponse("<h3>Artifact not found</h3>", status_code=404)
    return FileResponse(target)


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
