"""JSON API (``/api/*``) for the React/TanStack frontend (story-unraveler-tool).

Same-origin by design: the SPA is served from — or reverse-proxied alongside —
this app, so these endpoints reuse the existing Google-SSO **session cookie**
(no CORS, no tokens). Each returns the SAME real data the server-rendered pages
compute, via shared gatherers in :mod:`routes`, so the HTML and JSON surfaces
never drift. ``require_user`` returns HTTP 401 JSON when unauthenticated.
"""

from __future__ import annotations

import re as _re
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..context import RunContext
from ..trends import scoring
from . import routes
from .auth import require_user

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    """The signed-in user — lets the SPA render identity + gate admin controls."""
    u = require_user(request)
    return {"email": u.get("email"), "name": u.get("name"), "role": u.get("role"),
            "brands": u.get("brands") or []}


_LOGO_DOMAIN_RE = _re.compile(r"^[a-z0-9][a-z0-9.-]{1,79}$")


@router.get("/logo")
async def logo_proxy(request: Request, d: str, s: int = 64):
    """Cache-proxy for brand/OEM logos. Fetches each (domain,size) from logo.dev
    exactly ONCE, stores the bytes on disk, and serves every subsequent request
    from that cache — so we never re-spend a logo.dev request for a logo we've
    already seen, and the logo.dev token stays server-side. SSRF-safe: the domain
    is strictly validated and only ever interpolated into the logo.dev host."""
    require_user(request)
    import os
    from pathlib import Path

    from fastapi.responses import Response

    domain = (d or "").strip().lower()
    if "/" in domain or ".." in domain or not _LOGO_DOMAIN_RE.match(domain):
        raise HTTPException(status_code=400, detail="invalid domain")
    size = max(16, min(int(s or 64), 256))
    cache_dir = Path(os.environ.get("LOGO_CACHE_DIR", "logo_cache")).resolve()
    fp = cache_dir / f"{domain}_{size}.png"
    headers = {"Cache-Control": "public, max-age=31536000, immutable"}

    if fp.is_file():
        return Response(content=fp.read_bytes(), media_type="image/png", headers=headers)

    token = get_settings().logo_dev_token
    if not token:
        raise HTTPException(status_code=404, detail="logo source not configured")
    import httpx  # type: ignore
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            r = await c.get(f"https://img.logo.dev/{domain}",
                            params={"token": token, "size": size, "format": "png",
                                    "fallback": "monogram"})
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="logo fetch failed")
    if r.status_code != 200 or not r.content:
        raise HTTPException(status_code=404, detail="logo not found")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = fp.with_suffix(".tmp")
        tmp.write_bytes(r.content)
        tmp.replace(fp)                       # atomic publish into the cache
    except Exception:  # noqa: BLE001 — cache write is best-effort; still serve the bytes
        pass
    return Response(content=r.content, media_type="image/png", headers=headers)


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


@router.get("/personas")
async def personas_list(request: Request, brand: str | None = None) -> dict[str, Any]:
    """Writer-replication personas for a brand (§16.3) — used by the trigger
    screen's picker and the Writer Emulation manager."""
    u = require_user(request)
    from ..trends import personas as P
    settings = get_settings()
    b = brand or (list(settings.brand_keys)[0] if settings.brand_keys else "")
    async with RunContext.open() as ctx:
        rows = await P.list_personas(ctx.session, b)
    return {"brand": b,
            "personas": [{"id": p.id, "kind": p.kind, "name": p.name, "author": p.author,
                          "enabled": p.enabled, "has_features": bool(p.features),
                          "style_brief": p.style_brief} for p in rows],
            "may_edit": u.get("role") in ("global_admin", "portfolio_admin")}


@router.post("/personas/house")
async def personas_create_house(request: Request) -> dict[str, Any]:
    """Create a named house-style persona from a freeform style brief (admin)."""
    u = require_user(request)
    if u.get("role") not in ("global_admin", "portfolio_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    body = await _json_body(request)
    brand = (body.get("brand") or "").strip()
    name = (body.get("name") or "").strip()
    brief = (body.get("style_brief") or "").strip()
    if not (brand and name and brief):
        raise HTTPException(status_code=400, detail="brand, name and style_brief are required")
    settings = get_settings()
    if brand not in settings.brand_keys:
        raise HTTPException(status_code=400, detail=f"unknown brand '{brand}'")
    from ..trends import personas as P
    async with RunContext.open() as ctx:
        try:
            p = await P.create_house_persona(ctx.session, brand, name,
                                             style_brief=brief, created_by=u.get("email"))
        except IntegrityError:
            raise HTTPException(status_code=409, detail=f"a house persona named '{name}' already exists")
        return {"ok": True, "id": p.id, "name": p.name}


@router.post("/personas/distill")
async def personas_distill(request: Request) -> dict[str, Any]:
    """Distil a per-writer persona from a real top writer's corpus (admin). This
    scrapes + runs the LLM, so it can take a few seconds."""
    u = require_user(request)
    if u.get("role") not in ("global_admin", "portfolio_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    body = await _json_body(request)
    brand = (body.get("brand") or "").strip()
    author = (body.get("author") or "").strip()
    if not (brand and author):
        raise HTTPException(status_code=400, detail="brand and author are required")
    from ..agents.analytics import AnalyticsAgent
    async with RunContext.open() as ctx:
        pid = await AnalyticsAgent(ctx).distill_writer_persona(brand, author)
    if pid is None:
        raise HTTPException(status_code=502,
                            detail="couldn't distil this writer (not enough scrapable articles, or BigQuery/LLM unavailable)")
    return {"ok": True, "id": pid, "author": author}


@router.post("/personas/{persona_id}/enable")
async def personas_set_enabled(request: Request, persona_id: int) -> dict[str, Any]:
    """Toggle whether a persona is in the auto-rotation pool (admin)."""
    u = require_user(request)
    if u.get("role") not in ("global_admin", "portfolio_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    body = await _json_body(request)
    enabled = bool(body.get("enabled", True))
    from ..trends import personas as P
    async with RunContext.open() as ctx:
        p = await P.set_enabled(ctx.session, persona_id, enabled)
        if p is None:
            raise HTTPException(status_code=404, detail="persona not found")
        return {"ok": True, "id": p.id, "enabled": p.enabled}


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


@router.post("/cycle")
async def run_cycle(request: Request) -> dict[str, Any]:
    """Run the morning cycle (observe → plan) for one brand or all — the JSON
    sibling of the HTML /cycle form. Human-initiated; planning is dry-run by
    default and nothing dispatches without approval."""
    require_user(request)
    body = await _json_body(request)
    brand = (body.get("brand") or "all").strip() or "all"
    settings = get_settings()
    if brand != "all" and brand not in settings.brand_keys:
        raise HTTPException(status_code=400, detail=f"unknown brand '{brand}'")
    from datetime import date

    from ..orchestrator import run_morning_cycle
    from ..orchestrator.plans import PlanRepo
    brands = list(settings.brand_keys) if brand == "all" else [brand]
    for b in brands:
        await run_morning_cycle(b)
    plans = []
    async with RunContext.open() as ctx:
        repo = PlanRepo(ctx.session)
        for b in brands:
            latest = await repo.latest_plan(b, date.today())
            if latest is not None:
                plans.append({"brand": b, "plan_id": latest.id})
    return {"ok": True, "plans": plans}


# --- Trend Radar actions (JSON siblings of the HTML form routes) --------------
# Same business logic, same per-brand RBAC gate (``_can_approve``) as the
# server-rendered routes; the SPA calls these with ``credentials:'include'`` so
# the session cookie carries the caller's role. Approving/scanning spend money
# (LLM + paid source APIs), so they are gated to approvers, never viewers.


@router.post("/trends/scan")
async def trends_scan(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Kick a real trend scan in the background for the caller's brand scope."""
    require_user(request)
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
    persona_id = body.get("persona_id")   # None → auto-rotate
    persona_id = int(persona_id) if persona_id not in (None, "", "auto") else None
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
                                    content_types=picked, instructions=instructions,
                                    persona_id=persona_id)
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


# --- Distribution / Artifacts (§16) -------------------------------------------
# "Artifacts" are content_jobs across pipelines. These endpoints back the SPA's
# Distribution table + the artifact-review detail page with REAL data: the
# generated markdown body (via the artifact store), an LLM-scored quality +
# five-factor breakdown (scored lazily on first view and persisted to
# preview_meta), and fact-gate/voice signals. Plagiarism + est-traffic are
# pluggable (return None until their APIs are configured → SPA renders "n/a").

_DIST_STATUS = {
    "queued": "queued", "generating": "queued",
    "preview_ready": "review", "review": "review",
    "approved": "approved", "published": "published",
    "rejected": "rejected", "declined": "rejected",
    "cancelled": "rejected", "failed": "rejected",
}
_KIND_LABEL = {
    "article": "article", "social_post": "social post",
    "newsletter_blurb": "newsletter", "video_script": "video",
}


def _artifact_id(job_id: int) -> str:
    return f"AR-{job_id:04d}"


def _parse_artifact_id(s: str) -> int | None:
    try:
        return int(str(s).rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        return None


def _clean_title(t: str | None, fallback: str) -> str:
    import re
    t = re.sub(r"^[-*#>\s]+", "", (t or "").strip())
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t).strip().strip("*").strip()
    return t or fallback


def _artifact_row(job: Any) -> dict[str, Any]:
    """content_job → the Distribution list Artifact shape."""
    meta = job.preview_meta or {}
    brand = job.pipeline.brand if job.pipeline else "portfolio"
    quality = meta.get("quality") or {}
    return {
        "id": _artifact_id(job.id),
        "title": _clean_title(meta.get("title"), job.content_type.replace("_", " ")),
        "brand": brand,
        "kind": _KIND_LABEL.get(job.content_type, job.content_type.replace("_", " ")),
        "status": _DIST_STATUS.get(job.status, "queued"),
        "agent": "Production",
        "words": int(meta.get("word_count") or 0),
        "score": quality.get("score"),
    }


def _md_to_draft(md: str, *, title: str, brand: str, kind: str, generated: str,
                 artifact_id: str, hero_image: dict[str, Any] | None = None,
                 hero_images: dict[str, Any] | None = None) -> dict[str, Any]:
    """Parse a real markdown draft into the magazine-layout fields the website
    preview renders (dropcap lede → body → pull-quote → body). Keeps the exact
    Lovable layout, populated with the artifact's real text."""
    import re
    # Drop the SEO/meta section the writer prepends (## SEO … until the next heading).
    lines: list[str] = []
    skip_seo = False
    for ln in (md or "").splitlines():
        s = ln.strip()
        if re.match(r"^#{1,6}\s*SEO\b", s, re.I):
            skip_seo = True
            continue
        if skip_seo:
            if s.startswith("#"):
                skip_seo = False
            else:
                continue
        lines.append(ln)

    paras: list[str] = []
    quote: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if buf:
            p = " ".join(x.strip() for x in buf).strip()
            if len(p) > 1:
                paras.append(p)
        buf = []

    for ln in lines:
        s = ln.strip()
        if not s:
            flush()
            continue
        if s.startswith("#"):                       # heading → paragraph boundary
            flush()
            continue
        if re.match(r"^-{3,}$", s):                 # --- horizontal rule
            flush()
            continue
        if re.match(r"^-\s+\*\*.*\*\*\s*$", s):     # headline-option bullet (fully bold)
            continue
        if re.match(r"^\*\*[^*]+:\*\*", s):         # **Label:** metadata line
            continue
        if s.startswith(">"):
            flush()
            q = s.lstrip("> ").strip()
            if q and quote is None:
                quote = q
            continue
        s = re.sub(r"^[-*]\s+", "", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        s = re.sub(r"\*(.+?)\*", r"\1", s)
        s = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", s)  # markdown links → text
        buf.append(s)
    flush()

    lede = paras[0] if paras else title
    rest = paras[1:]
    if quote is None and rest:
        sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", " ".join(rest)) if len(x.strip()) > 30]
        quote = max(sentences, key=len) if sentences else None
    mid = max(1, len(rest) // 2)
    kicker = {"article": "Feature", "social_post": "Social",
              "newsletter_blurb": "Newsletter", "video_script": "Video"}.get(kind, "Draft")
    return {
        "kicker": kicker,
        "headline": title,
        "byline": f"By {brand.title()} AI",
        "dateline": generated,
        "domain": f"{brand}.com",
        "slug": f"drafts/{artifact_id.lower()}",
        "heroGradient": "linear-gradient(135deg,#1a1a1a,#3a3a3a)",
        # Operator-chosen hero image (via the picker) — full URL + attribution;
        # None until one is selected, so the preview falls back to the gradient.
        # heroImage is the default/website hero; heroImages holds per-channel
        # overrides (each channel's preview can carry its own picture).
        "heroImage": (hero_image or {}).get("url"),
        "heroCredit": (hero_image or {}).get("credit"),
        "heroCreditUrl": (hero_image or {}).get("credit_url"),
        "heroSource": (hero_image or {}).get("source"),
        "heroImages": hero_images or {},
        "heroBadge": f"{len((md or '').split())} words",
        "dropcap": (lede[:1] or "T").upper(),
        "lede": lede[1:] if lede else "",
        "bodyOne": " ".join(rest[:mid]) if rest else "",
        "pullQuote": quote or "",
        "bodyTwo": " ".join(rest[mid:]) if len(rest) > mid else "",
    }


_BREAKDOWN_LABELS = [
    ("factuality", "Factuality", "30%"), ("editorial_fit", "Editorial fit", "25%"),
    ("freshness", "Freshness", "20%"), ("seo_ceiling", "SEO ceiling", "15%"),
    ("brand_voice", "Brand voice", "10%"),
]


def _breakdown_rows(breakdown: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"label": lbl, "value": breakdown.get(k), "weight": w}
            for k, lbl, w in _BREAKDOWN_LABELS]


def _artifact_signals(breakdown: dict[str, Any], gate: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "fact_gate": gate["label"] if gate else None,
        "voice_match": breakdown.get("brand_voice"),
        "plagiarism": _plagiarism_signal(),
        "seo_ceiling": breakdown.get("seo_ceiling"),
        "est_traffic": _traffic_signal(),
    }


def _plagiarism_signal() -> str | None:
    """Pluggable — wired when a plagiarism API (Copyscape/Originality.ai) is
    configured. None → the SPA renders 'n/a'."""
    return None


def _traffic_signal() -> str | None:
    """Pluggable — wired when an SEO/traffic API (Semrush/Ahrefs/GA4) is
    configured. None → the SPA renders 'n/a'."""
    return None


def _artifact_timeline(job: Any, row: dict[str, Any]) -> list[dict[str, Any]]:
    tl: list[dict[str, Any]] = []
    micros = (job.cost or {}).get("llm_micros")
    usd = f" · ${micros / 1e6:.2f} LLM" if micros else ""
    tl.append({"at": routes._fmt_dt(job.created_at), "actor": f"{row['agent']} agent",
               "event": "draft generated", "detail": f"{row['words']} words{usd}"})
    for h in (job.history or []):
        tl.append({"at": routes._fmt_dt(h.get("at")) if h.get("at") else "",
                   "actor": "editorial", "event": "regenerated",
                   "detail": h.get("instructions") or ""})
    if job.reviewed_by:
        tl.append({"at": routes._fmt_dt(job.reviewed_at), "actor": job.reviewed_by,
                   "event": job.status, "detail": ""})
    return tl


async def _ensure_quality(ctx: RunContext, job: Any, body: str) -> dict[str, Any]:
    """Return the job's stored quality; if missing, LLM-score the body once and
    persist it to preview_meta so future reads are free."""
    meta = dict(job.preview_meta or {})
    quality = meta.get("quality")
    if quality or not body:
        return quality or {}
    brand = job.pipeline.brand if job.pipeline else "portfolio"
    scored = await scoring.score_draft(ctx, brand, job.content_type, body)
    if not scored:
        return {}
    quality = {"score": scored["score"], "breakdown": scored["breakdown"], "note": scored["note"]}
    meta["quality"] = quality
    job.preview_meta = meta
    await ctx.session.flush()
    return quality


@router.get("/artifacts")
async def artifacts_list(request: Request, brand: str | None = None) -> dict[str, Any]:
    """Distribution: content artifacts staged for review, newest first."""
    require_user(request)
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from ..db.models import ContentJob, ContentPipeline
    async with RunContext.open() as ctx:
        q = (select(ContentJob)
             .options(selectinload(ContentJob.pipeline))
             .join(ContentPipeline, ContentPipeline.id == ContentJob.pipeline_id)
             .where(ContentJob.status != "cancelled")
             .order_by(ContentJob.id.desc()).limit(50))
        if brand:
            q = q.where(ContentPipeline.brand == brand)
        jobs = list((await ctx.session.execute(q)).scalars().all())
        # Lazily score any unscored jobs (bounded) so the table shows real scores.
        scored_budget = 12
        for j in jobs:
            meta = j.preview_meta or {}
            if not (meta.get("quality")) and j.preview_ref and scored_budget > 0:
                body = routes._artifact_text(j.preview_ref)
                if body:
                    await _ensure_quality(ctx, j, body)
                    scored_budget -= 1
        rows = [_artifact_row(j) for j in jobs]
    counts = {
        "review": sum(1 for r in rows if r["status"] == "review"),
        "rejected": sum(1 for r in rows if r["status"] == "rejected"),
        "published": sum(1 for r in rows if r["status"] == "published"),
    }
    return {"artifacts": rows, "counts": counts}


@router.get("/artifacts/{artifact_id}")
async def artifact_detail(request: Request, artifact_id: str) -> dict[str, Any]:
    """One artifact: metadata + parsed article draft + score breakdown + signals
    + timeline — the full artifact-review surface, on real data."""
    require_user(request)
    job_id = _parse_artifact_id(artifact_id)
    if job_id is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from ..db.models import ContentJob, ContentPipeline
    async with RunContext.open() as ctx:
        job = (await ctx.session.execute(
            select(ContentJob)
            .options(selectinload(ContentJob.pipeline).selectinload(ContentPipeline.trend))
            .where(ContentJob.id == job_id))).scalar_one_or_none()
        if job is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        brand = job.pipeline.brand if job.pipeline else "portfolio"
        trend = job.pipeline.trend if job.pipeline else None
        body = routes._artifact_text(job.preview_ref) if job.preview_ref else ""
        quality = await _ensure_quality(ctx, job, body)
        breakdown = quality.get("breakdown") or {}
        row = _artifact_row(job)
        generated = routes._fmt_dt(job.created_at)
        detail = {
            **row,
            "generated_at": generated,
            "cost": job.cost,
            "note": quality.get("note"),
            "article": _md_to_draft(body, title=row["title"], brand=brand,
                                    kind=job.content_type, generated=generated, artifact_id=row["id"],
                                    hero_image=((job.preview_meta or {}).get("hero_images") or {}).get("web")
                                               or (job.preview_meta or {}).get("hero_image"),
                                    hero_images=(job.preview_meta or {}).get("hero_images") or {}),
            "breakdown": _breakdown_rows(breakdown),
            "signals": _artifact_signals(breakdown, scoring.fact_gate(trend)),
            "timeline": _artifact_timeline(job, row),
            "may_approve": routes._can_approve(request, brand),
        }
    return detail


def _image_query(trend: Any, title: str) -> str:
    """A short, image-search-friendly query for an artifact: the trend's OEM
    anchor + a couple of headline words, else the title. Stock APIs match short
    concrete phrases best."""
    if trend is not None:
        oems = ((trend.entities or {}).get("oems") if getattr(trend, "entities", None) else None) or []
        head = (trend.headline or "").strip()
        if head:
            # Drop a trailing "… recall/recalls …" clause for a cleaner image query.
            q = head.split(" recall")[0].strip() or head
            # Prepend the OEM only if the headline doesn't already name it.
            if oems and oems[0].lower() not in q.lower():
                q = f"{oems[0]} {q}"
            return q[:60]
    return (title or "automotive").strip()[:60]


@router.get("/artifacts/{artifact_id}/image-candidates")
async def artifact_image_candidates(request: Request, artifact_id: str,
                                    q: str | None = None, refresh: bool = False) -> dict[str, Any]:
    """Hero-image candidates for an artifact — the brand media library + Unsplash
    + Pexels, searched by ``q`` (defaults to the trend's topic).

    Cached per artifact on ``preview_meta`` so a page reload reuses the same set
    (no repeat stock-API calls / rate-limit burn). The cache is reused only while
    the query matches; a new search fetches fresh, and ``refresh=1`` forces a new
    fetch (the picker's Refresh button). Sources with no key are skipped; the
    `sources` map reports each one's state."""
    require_user(request)
    job_id = _parse_artifact_id(artifact_id)
    if job_id is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from ..adapters.images import image_candidates
    from ..db.models import ContentJob, ContentPipeline
    async with RunContext.open() as ctx:
        job = (await ctx.session.execute(
            select(ContentJob)
            .options(selectinload(ContentJob.pipeline).selectinload(ContentPipeline.trend))
            .where(ContentJob.id == job_id))).scalar_one_or_none()
        if job is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        trend = job.pipeline.trend if job.pipeline else None
        title = (job.preview_meta or {}).get("title") or f"artifact {artifact_id}"
        query = (q or "").strip() or _image_query(trend, title)

        cache = (job.preview_meta or {}).get("image_candidates")
        same_query = isinstance(cache, dict) and cache.get("query") == query
        if not refresh and same_query and cache.get("candidates"):
            return {"query": query, "candidates": cache["candidates"],
                    "sources": cache.get("sources", {}), "cached": True}

        # Refresh advances the result page (cycling 1→5) to pull a genuinely
        # different set; a new query starts back at page 1.
        page = 1
        if refresh and same_query:
            page = (int(cache.get("page", 1)) % 5) + 1
        result = await image_candidates(ctx.creds, query, page=page)
        # Cache only a non-empty fetch — so a transient/keyless empty result
        # doesn't get pinned (a later load re-tries and picks up new keys).
        if result.get("candidates"):
            meta = dict(job.preview_meta or {})
            meta["image_candidates"] = {"query": result["query"],
                                        "candidates": result["candidates"],
                                        "sources": result["sources"],
                                        "page": result.get("page", page)}
            job.preview_meta = meta
            await ctx.session.flush()
    return {**result, "cached": False}


@router.post("/artifacts/{artifact_id}/hero-image")
async def artifact_set_hero_image(request: Request) -> dict[str, Any]:
    """Persist (or clear) a chosen hero image for one preview channel. Body:
    {channel, url, credit, credit_url, source} — or {url: null} to clear that
    channel. Stored per-channel on preview_meta.hero_images so each preview can
    carry its own picture; the ``web`` channel also syncs the legacy hero_image
    (the article/publish default)."""
    u = require_user(request)
    artifact_id = request.path_params["artifact_id"]
    job_id = _parse_artifact_id(artifact_id)
    if job_id is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    body = await _json_body(request)
    channel = (body.get("channel") or "web").strip() or "web"
    from sqlalchemy import select

    from ..db.models import ContentJob
    async with RunContext.open() as ctx:
        job = (await ctx.session.execute(
            select(ContentJob).where(ContentJob.id == job_id))).scalar_one_or_none()
        if job is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        meta = dict(job.preview_meta or {})
        images = dict(meta.get("hero_images") or {})
        url = body.get("url")
        obj = None
        if url:
            obj = {
                "url": str(url),
                "credit": (body.get("credit") or None),
                "credit_url": (body.get("credit_url") or None),
                "source": (body.get("source") or None),
                "chosen_by": u.get("email"),
            }
            images[channel] = obj
        else:
            images.pop(channel, None)
        meta["hero_images"] = images
        # Keep the legacy single hero_image (article/publish default) == the web
        # channel's image.
        if channel == "web":
            if obj:
                meta["hero_image"] = obj
            else:
                meta.pop("hero_image", None)
        job.preview_meta = meta          # reassign so SQLAlchemy flags the JSONB dirty
        await ctx.session.flush()
    return {"ok": True, "channel": channel, "hero_image": obj, "hero_images": images}


# --- Distribution (§6.6 outbound artifacts) -----------------------------------
# The real distribution surface: assembled outbound artifacts staged for human
# review (daily digest / newsletter / social) + trend drafts. Draft + human-send
# only — nothing distributes autonomously. Same source as the HTML /distribution
# page (memory REPORT + DISTRIBUTION_DRAFT entries).

_DISTRIBUTION_KINDS = {
    "daily_digest_inputs": ("Daily digest", "digest"),
    "daily_digest": ("Daily digest", "digest"),
    "newsletter_draft": ("CarBuzz newsletter", "newsletter"),
    "social_draft": ("Social posts", "social"),
}


@router.get("/distribution")
async def distribution(request: Request) -> dict[str, Any]:
    """§6.6 outbound artifacts staged for human review — digest/newsletter/social
    + trend drafts, with per-channel counts. Nothing sends autonomously."""
    require_user(request)
    from ..db.enums import EntryType
    async with RunContext.open() as ctx:
        rows = await ctx.store.query(types=[EntryType.REPORT, EntryType.DISTRIBUTION_DRAFT],
                                     status=None, limit=100)
        items = []
        for r in rows:
            p = r.payload or {}
            kind = p.get("kind", "")
            label, channel = _DISTRIBUTION_KINDS.get(kind, (kind, "other"))
            ref = p.get("artifact_ref") if isinstance(p.get("artifact_ref"), dict) else None
            url = None
            if ref and ref.get("backend") == "local" and ref.get("key"):
                url = f"/api/artifact-file/{ref['key']}"
            elif ref:
                url = ref.get("uri")
            items.append({
                "id": r.id, "brand": r.brand, "label": label, "channel": channel,
                "type": r.type.value,
                "status": p.get("status") or ("ready" if p.get("ready") else "inputs"),
                "artifact_url": url,
                "bytes": ref.get("bytes") if ref else None,
                "created_at": routes._fmt_dt(r.created_at),
            })
    channels = {
        "digest": sum(1 for i in items if i["channel"] == "digest"),
        "newsletter": sum(1 for i in items if i["channel"] == "newsletter"),
        "social": sum(1 for i in items if i["channel"] == "social"),
    }
    assembled = sum(1 for i in items if i["status"] in ("assembled", "ready"))
    return {"items": items, "channels": channels, "assembled": assembled}


@router.get("/artifact-file/{key:path}")
async def artifact_file(request: Request, key: str):
    """Serve a locally-stored artifact (read-only, path-traversal-guarded).
    Mirrors the HTML /artifacts/{key} route but under /api so it never collides
    with the SPA's client-side /artifacts/:id review route."""
    require_user(request)
    from pathlib import Path

    from fastapi.responses import FileResponse, HTMLResponse
    root = Path(get_settings().artifacts.local_dir).resolve()
    target = (root / key).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return HTMLResponse("<h3>Artifact not found</h3>", status_code=404)
    return FileResponse(target)


async def _json_body(request: Request) -> dict[str, Any]:
    """Tolerant JSON-body read — the SPA may POST an empty body for no-arg actions."""
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# ==============================================================================
# Remaining SPA surfaces — each endpoint reuses the HTML page's gatherers (or
# the same tables) so the JSON and server-rendered views never drift. Where the
# design shows a value the backend can't source, the endpoint returns null and
# the SPA renders a placeholder — never a fabricated number.
# ==============================================================================


@router.get("/observability")
async def observability(request: Request, limit: int = 60) -> dict[str, Any]:
    """Live event stream + KPI pills. latency_p95_ms is always null — no
    duration column exists on tool_call_log (honest placeholder)."""
    require_user(request)
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import case, func, select

    from ..db.models import ToolCallLog
    limit = min(max(limit, 1), 200)
    now = datetime.now(timezone.utc)
    async with RunContext.open() as ctx:
        raw = await routes._activity_events(ctx, limit)
        failed, completed = (await ctx.session.execute(
            select(func.coalesce(func.sum(case((ToolCallLog.ok.is_(False), 1), else_=0)), 0),
                   func.count())
            .where(ToolCallLog.ok.is_not(None),
                   ToolCallLog.created_at >= now - timedelta(hours=24)))).one()
        req_1h = (await ctx.session.execute(
            select(func.count()).select_from(ToolCallLog)
            .where(ToolCallLog.created_at >= now - timedelta(hours=1)))).scalar_one()
    events = []
    for i, ev in enumerate(raw):
        when = ev.get("when") or ""
        ts = when.split(" ")[1] if " " in when else (ev.get("ago") or "")
        level = {"bad": "error", "warn": "warn"}.get(ev.get("severity"), "info")
        agent, system = ev.get("agent"), ev.get("system")
        source = f"{agent}.{system}" if agent and system else (agent or system or "system")
        events.append({"id": f"E-{i}", "ts": ts, "level": level, "source": source,
                       "message": ev.get("message")})
    error_rate = round(100 * int(failed) / int(completed), 1) if completed else None
    return {"kpis": {"error_rate_pct": error_rate, "latency_p95_ms": None,
                     "requests_1h": int(req_1h)},
            "events": events}


# --- Users (admin) -------------------------------------------------------------


@router.get("/users")
async def users_list(request: Request) -> dict[str, Any]:
    """User directory — global-admin only, mirroring the HTML /users gate."""
    u = require_user(request)
    from ..rbac import ROLE_LABELS, Role, can_manage_users
    from ..users import UserRepo
    if not can_manage_users(u.get("role", "viewer")):
        raise HTTPException(status_code=403, detail="global admin only")
    settings = get_settings()
    async with RunContext.open() as ctx:
        rows = await UserRepo(ctx.session).list()
        users = [{"email": r.email, "name": r.name, "role": r.role,
                  "role_label": ROLE_LABELS.get(r.role, r.role), "brands": r.brands or [],
                  # No invited/revoked lifecycle exists; no last-seen tracking → null.
                  "status": "active", "last_seen": None,
                  "created_at": r.created_at.strftime("%Y-%m-%d") if r.created_at else ""}
                 for r in rows]
    counts = {r.value: 0 for r in Role}
    for x in users:
        counts[x["role"]] = counts.get(x["role"], 0) + 1
    return {"users": users, "role_counts": counts, "roles": [r.value for r in Role],
            "role_labels": dict(ROLE_LABELS), "brands": list(settings.brand_keys)}


@router.post("/users/invite")
async def users_invite(request: Request) -> dict[str, Any]:
    """Pre-assign a role to an email (applies at first sign-in). No email is
    actually sent — there is no mail infra; this provisions + sets the role."""
    u = require_user(request)
    from ..rbac import can_manage_users, is_valid_role
    from ..users import UserRepo
    if not can_manage_users(u.get("role", "viewer")):
        raise HTTPException(status_code=403, detail="global admin only")
    body = await _json_body(request)
    email = str(body.get("email") or "").lower().strip()
    role = str(body.get("role") or "").strip()
    brands = body.get("brands") or None
    if "@" not in email:
        raise HTTPException(status_code=400, detail="valid email required")
    if not is_valid_role(role):
        raise HTTPException(status_code=400, detail="invalid role")
    async with RunContext.open() as ctx:
        repo = UserRepo(ctx.session)
        await repo.provision(email, None)
        try:
            await repo.set_role(email, role, brands)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "email": email, "role": role}


@router.post("/users/set-role")
async def users_set_role(request: Request) -> dict[str, Any]:
    """JSON sibling of the HTML set-role form action."""
    u = require_user(request)
    from ..rbac import can_manage_users, is_valid_role
    from ..users import UserRepo
    if not can_manage_users(u.get("role", "viewer")):
        raise HTTPException(status_code=403, detail="global admin only")
    body = await _json_body(request)
    email = str(body.get("email") or "").lower().strip()
    role = str(body.get("role") or "").strip()
    if not email or not is_valid_role(role):
        raise HTTPException(status_code=400, detail="email + valid role required")
    async with RunContext.open() as ctx:
        try:
            await UserRepo(ctx.session).set_role(email, role, body.get("brands") or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


# --- Trend Score weights (§13.19) -----------------------------------------------


@router.get("/trend-score-weights")
async def get_trend_score_weights(request: Request) -> dict[str, Any]:
    """The Trend Score formula's weights: effective value + shipped default +
    editor metadata (label/help/range/sign) per weight. `may_edit` gates the
    editor to admins."""
    u = require_user(request)
    from ..trends.detector import DEFAULT_SCORE_WEIGHTS, SCORE_WEIGHT_META
    from ..trends.weights import load_overrides
    async with RunContext.open() as ctx:
        overrides = await load_overrides(ctx.session)
    weights = [{**m,
                "value": round(overrides.get(m["key"], DEFAULT_SCORE_WEIGHTS[m["key"]]), 4),
                "default": DEFAULT_SCORE_WEIGHTS[m["key"]],
                "customized": m["key"] in overrides}
               for m in SCORE_WEIGHT_META]
    return {"weights": weights, "customized": bool(overrides),
            "may_edit": u.get("role") in ("global_admin", "portfolio_admin")}


@router.post("/trend-score-weights")
async def set_trend_score_weights(request: Request) -> dict[str, Any]:
    """Persist new Trend Score weights (admin only). Body: {"weights": {key: value}}.
    Each value is clamped to its range; unchanged keys are skipped. Takes effect
    on the next Trend Scout scan."""
    u = require_user(request)
    if u.get("role") not in ("global_admin", "portfolio_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    body = await _json_body(request)
    values = body.get("weights")
    if not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="weights object required")
    from ..trends.weights import save_weights
    async with RunContext.open() as ctx:
        written = await save_weights(ctx.session, values, updated_by=u.get("email"))
    return {"ok": True, "updated": written}


@router.post("/trend-score-weights/reset")
async def reset_trend_score_weights(request: Request) -> dict[str, Any]:
    """Reset every Trend Score weight to its shipped default (admin only)."""
    u = require_user(request)
    if u.get("role") not in ("global_admin", "portfolio_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    from ..trends.weights import reset_weights
    async with RunContext.open() as ctx:
        n = await reset_weights(ctx.session, updated_by=u.get("email"))
    return {"ok": True, "reset": n}


# --- Integrations / notification config -----------------------------------------


def _require_admin(request: Request) -> dict[str, Any]:
    u = require_user(request)
    if u.get("role") not in ("global_admin", "portfolio_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    return u


@router.get("/notification-config")
async def notification_config_get(request: Request) -> dict[str, Any]:
    """Admin config for outbound integrations. Currently the trend-alert webhook,
    which fires when the Scout surfaces a trend scoring >= min_score."""
    _require_admin(request)
    from ..notifications import load_trend_alert
    async with RunContext.open() as ctx:
        trend_alert = await load_trend_alert(ctx.session)
    return {"trend_alert": trend_alert}


@router.post("/notification-config")
async def notification_config_set(request: Request) -> dict[str, Any]:
    """Persist the trend-alert webhook config (admin only). Body:
    {"trend_alert": {"enabled": bool, "webhook_url": str, "min_score": number}}."""
    u = _require_admin(request)
    body = await _json_body(request)
    ta = body.get("trend_alert") if isinstance(body.get("trend_alert"), dict) else body
    ta = ta if isinstance(ta, dict) else {}
    from ..notifications import save_trend_alert
    try:
        async with RunContext.open() as ctx:
            saved = await save_trend_alert(
                ctx.session, enabled=ta.get("enabled"), webhook_url=ta.get("webhook_url"),
                min_score=ta.get("min_score"), updated_by=u.get("email"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "trend_alert": saved}


@router.post("/notification-config/test")
async def notification_config_test(request: Request) -> dict[str, Any]:
    """Fire a synthetic test ping at the webhook (admin only). Uses the URL in
    the request body if given, otherwise the saved one."""
    _require_admin(request)
    body = await _json_body(request)
    from ..notifications import load_trend_alert, send_test_ping
    url = str((body or {}).get("webhook_url") or "").strip()
    if not url:
        async with RunContext.open() as ctx:
            url = (await load_trend_alert(ctx.session))["webhook_url"]
    if not url:
        raise HTTPException(status_code=400, detail="no webhook_url configured")
    ok, detail = await send_test_ping(url)
    return {"ok": ok, "detail": detail}


# --- Governor -------------------------------------------------------------------


@router.get("/governor")
async def governor(request: Request) -> dict[str, Any]:
    """Kill switch, the three real metered caps, spend snapshot, and the
    code-enforced guardrails (as data, so nothing fabricated renders)."""
    require_user(request)
    settings = get_settings()
    async with RunContext.open() as ctx:
        spend = await routes._spend_snapshot(ctx)
        deltas = await routes._spend_deltas(ctx, spend)
        resources = await routes._resource_usage(ctx, spend, deltas)

    def cap_row(name: str, metric: str, fmt) -> dict[str, Any]:
        cap = settings.caps.per_day(metric)
        spent = spend[metric]["spent_today"]
        return {"name": name, "scope": "portfolio",
                "limit": fmt(cap) if cap else "no cap set",
                "used": fmt(spent),
                "pct": int(round(spend[metric]["pct"] or 0))}

    caps = [
        cap_row("LLM daily spend", "llm_micros", lambda v: f"${(v or 0) / 1e6:.2f}"),
        cap_row("BigQuery scan", "bq_bytes", routes._fmt_bytes),
        cap_row("Ahrefs units", "ahrefs_units", lambda v: f"{int(v or 0):,}"),
    ]
    rules = [
        {"id": "approval_gate", "name": "Require human approval before CMS publish",
         "description": "No dispatch to Valnet CMS without an editor click-through.",
         "enabled": True},
        {"id": "dry_run_default", "name": "Dry-run by default on live actions",
         "description": "Action adapters run dry unless a run explicitly requests live.",
         "enabled": settings.dry_run_default},
        {"id": "daily_caps", "name": "Hard-stop at daily spend caps",
         "description": "Governor blocks metered calls once a daily cap is hit until reset.",
         "enabled": settings.caps.enabled, "threshold": 100, "unit": "%"},
        {"id": "kill_switch", "name": "Kill switch halts dispatch",
         "description": "Observe still runs; nothing dispatches while engaged.",
         "enabled": settings.kill_switch},
    ]
    return {"kill_switch": settings.kill_switch, "caps_enabled": settings.caps.enabled,
            "caps": caps, "resources": resources, "rules": rules}


# --- Morning plans + plan-item approvals ----------------------------------------


def _item_title(it: dict[str, Any]) -> str:
    params = it.get("params") or {}
    for k in ("title", "name", "message"):
        v = params.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return str(it.get("action_type") or "").replace("_", " ").capitalize()


def _linked_memory(params: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if params.get("trend_id") is not None:
        out.append(f"trend:{params['trend_id']}")
    if params.get("topic_id") is not None:
        out.append(f"topic:{params['topic_id']}")
    for k in ("report_entry_id", "draft_entry_id"):
        if params.get(k) is not None:
            out.append(f"memory:{params[k]}")
    return out


def _morning_plan_dict(plan: Any, rates: dict[str, float]) -> dict[str, Any]:
    """routes._plan_dict + the SPA's derived fields (USD costs, titles, brief)."""
    from .. import pricing
    base = routes._plan_dict(plan)
    proposed = 0
    total_usd = 0.0
    for it in base["items"]:
        est = it.get("cost_estimate") or {}
        usd = round(sum(pricing.metric_to_usd(m, amt or 0, bq_tb=rates["bq_tb"],
                                              ahrefs_unit=rates["ahrefs_unit"])
                        for m, amt in est.items()), 2)
        conv: dict[str, Any] = {}
        if est.get("ahrefs_units"):
            conv["ahrefs_units"] = int(est["ahrefs_units"])
        if est.get("llm_micros"):
            conv["llm_usd"] = round(est["llm_micros"] / 1e6, 2)
        if est.get("bq_bytes"):
            conv["bq_mb"] = round(est["bq_bytes"] / 1048576)
        it["cost_estimate"] = conv
        it["cost_usd"] = usd
        it["brand"] = base["brand"]
        it["title"] = _item_title(it)
        it["rationale"] = it.get("rationale") or ""
        it["params"] = it.get("params") or {}
        it["linked_memory"] = _linked_memory(it["params"])
        if it["status"] == "proposed":
            proposed += 1
        total_usd += usd
    # No slack_brief column exists — deterministic summary from real row data only.
    base["slack_brief"] = (f"{base['brand'].title()} • {proposed} proposed item(s) · "
                           f"est ${total_usd:.2f} · dry-run by default.")
    return base


@router.get("/plans")
async def plans_index(request: Request) -> dict[str, Any]:
    """Morning-plan overview: the latest plan per brand."""
    require_user(request)
    from .. import pricing
    from ..orchestrator.plans import PlanRepo
    settings = get_settings()
    async with RunContext.open() as ctx:
        rates = await pricing.load_rates(ctx.session)
        repo = PlanRepo(ctx.session)
        out = []
        for b in settings.brand_keys:
            p = await repo.latest_plan(b)
            if p is not None:
                out.append(_morning_plan_dict(p, rates))
    return {"plans": out}


@router.get("/plans/{plan_id}")
async def plan_detail(request: Request, plan_id: int) -> dict[str, Any]:
    require_user(request)
    from .. import pricing
    from ..orchestrator.plans import PlanRepo
    async with RunContext.open() as ctx:
        plan = await PlanRepo(ctx.session).get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        rates = await pricing.load_rates(ctx.session)
        out = _morning_plan_dict(plan, rates)
        out["can_approve"] = routes._can_approve(request, out["brand"])
    return out


@router.post("/items/{item_id}/approve")
@router.post("/approvals/{item_id}/approve")
async def item_approve_api(request: Request, item_id: int) -> dict[str, Any]:
    """Approve one plan item (both the morning-plan and approvals screens call
    this). Plan looked up server-side from the item — never a caller param."""
    user = require_user(request)
    body = await _json_body(request)
    go_live = bool(body.get("go_live"))
    from ..db.models import PlanItem
    from ..orchestrator.plans import ApprovalError, PlanRepo
    async with RunContext.open() as ctx:
        item = await ctx.session.get(PlanItem, item_id)
        if item is None or item.plan_id is None:
            raise HTTPException(status_code=404, detail="plan item not found")
        plan = await PlanRepo(ctx.session).get_plan(item.plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        if not routes._can_approve(request, plan.brand):
            raise HTTPException(status_code=403, detail="not permitted for this brand")
        try:
            await PlanRepo(ctx.session).approve_item(item_id, user["email"], go_live=go_live)
        except ApprovalError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "plan_item_id": item_id, "status": "approved", "dry_run": not go_live}


@router.post("/items/{item_id}/reject")
@router.post("/approvals/{item_id}/reject")
async def item_reject_api(request: Request, item_id: int) -> dict[str, Any]:
    user = require_user(request)
    from ..db.models import PlanItem
    from ..orchestrator.plans import ApprovalError, PlanRepo
    async with RunContext.open() as ctx:
        item = await ctx.session.get(PlanItem, item_id)
        if item is None or item.plan_id is None:
            raise HTTPException(status_code=404, detail="plan item not found")
        plan = await PlanRepo(ctx.session).get_plan(item.plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        if not routes._can_approve(request, plan.brand):
            raise HTTPException(status_code=403, detail="not permitted for this brand")
        try:
            await PlanRepo(ctx.session).reject_item(item_id, user["email"])
        except ApprovalError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "plan_item_id": item_id, "status": "rejected"}


@router.post("/plans/{plan_id}/dispatch")
async def plan_dispatch_api(request: Request, plan_id: int) -> dict[str, Any]:
    """Dispatch an approved plan (JSON sibling of the HTML dispatch action)."""
    require_user(request)
    from ..orchestrator.dispatch import DispatchError, Dispatcher
    from ..orchestrator.plans import PlanRepo
    async with RunContext.open() as ctx:
        plan = await PlanRepo(ctx.session).get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        if not routes._can_approve(request, plan.brand):
            raise HTTPException(status_code=403, detail="not permitted for this brand")
        try:
            summary = await Dispatcher(ctx).dispatch_plan(plan_id)
        except DispatchError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "summary": summary}


# --- Approvals queue -------------------------------------------------------------

# Deterministic risk tier per action_type (mirrors the dispatcher's side-effect
# surface: external publish/send = high; external task/spend = medium; internal
# generation = low). Derived from a real column — documented, not fabricated.
_ACTION_RISK = {
    "emaki_publish_draft": "high", "send_digest_email": "high",
    "create_asana_task": "medium", "route_to_writer": "medium", "queue_decay_refresh": "medium",
    "trigger_ideation": "low", "assemble_newsletter": "low", "assemble_social_post": "low",
    "assemble_digest": "low", "notify": "low",
}


@router.get("/approvals")
async def approvals_list(request: Request, brand: str | None = None,
                         limit: int = 50) -> dict[str, Any]:
    """The undecided queue: every proposed plan item across recent plans."""
    require_user(request)
    from datetime import datetime, timezone

    from .. import pricing
    from ..orchestrator.plans import PlanRepo
    limit = min(max(limit, 1), 200)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    async with RunContext.open() as ctx:
        rates = await pricing.load_rates(ctx.session)
        plans = await PlanRepo(ctx.session).list_plans(50)
        rows = []
        for plan in plans:
            if brand and plan.brand != brand:
                continue
            for item in plan.items or []:
                if item.status != "proposed":
                    continue
                est = item.cost_estimate or {}
                usd = round(sum(pricing.metric_to_usd(m, amt or 0, bq_tb=rates["bq_tb"],
                                                      ahrefs_unit=rates["ahrefs_unit"])
                                for m, amt in est.items()), 2)
                rows.append({
                    "id": str(item.id), "ts": routes._ago(item.created_at),
                    "brand": plan.brand, "plan_id": plan.id, "plan_item_id": item.id,
                    "action_type": item.action_type,
                    "title": (item.rationale or
                              item.action_type.replace("_", " ").capitalize()),
                    "requested_by": plan.created_by, "cost_usd": usd,
                    "risk": _ACTION_RISK.get(item.action_type, "medium"),
                    "dry_run": bool(item.dry_run),
                    "may_approve": routes._can_approve(request, plan.brand),
                    "_created": item.created_at or epoch,
                })
    rows.sort(key=lambda r: r["_created"], reverse=True)
    for r in rows:
        r.pop("_created", None)
    return {"approvals": rows[:limit]}


# --- Memory ledger ---------------------------------------------------------------
# Path is /memory/ledger (not /memory): routes.py's legacy /api/memory CSV export
# registers first and would shadow a plain /memory here.


@router.get("/memory/ledger")
async def memory_ledger(request: Request, brand: str | None = None,
                        type: str | None = None, agent: str | None = None,
                        verified: bool | None = None, limit: int = 50) -> dict[str, Any]:
    require_user(request)
    from sqlalchemy import func, select

    from ..db.enums import EntryType
    from ..db.models import MemoryEntry
    limit = min(max(limit, 1), 200)
    types = None
    if type:
        try:
            types = [EntryType(type)]
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown type '{type}'")
    async with RunContext.open() as ctx:
        rows = await ctx.store.query(brand=brand or None, include_portfolio=True,
                                     types=types, source_agent=agent or None,
                                     verified=verified, status=None, limit=limit)
        counts = dict((await ctx.session.execute(
            select(MemoryEntry.source_agent, func.count())
            .where(MemoryEntry.status == "active")
            .group_by(MemoryEntry.source_agent))).all())
        total = (await ctx.session.execute(
            select(func.count()).select_from(MemoryEntry)
            .where(MemoryEntry.status == "active"))).scalar_one()
    entries = []
    for r in rows:
        p = r.payload or {}
        subject = (p.get("statement") or p.get("headline") or p.get("summary")
                   or p.get("title") or p.get("note") or p.get("url")
                   or p.get("kind") or r.type.value)
        ago = routes._ago(r.created_at)
        if ago.endswith(" ago"):        # the SPA appends " ago" itself
            ago = ago[:-4]
        entries.append({"id": f"M-{r.id}", "entry_id": r.id, "agent": r.source_agent,
                        "type": r.type.value, "subject": subject, "brand": r.brand,
                        "ago": ago,
                        "facts": None,   # no per-entry fact count exists → SPA renders "—"
                        "sources": len(r.source_urls or []),
                        "verified": bool(r.verified), "kind": p.get("kind")})
    seen: set[str] = set()
    by_agent = []
    for meta in (*routes.AGENT_META, *routes.FEEDER_META):
        k = meta["key"]
        seen.add(k)
        by_agent.append({"key": k, "display": meta["display"],
                         "entries": int(counts.get(k, 0))})
    for k, c in counts.items():
        if k not in seen:
            by_agent.append({"key": k, "display": k, "entries": int(c)})
    return {"entries": entries, "by_agent": by_agent, "total": int(total)}


# --- Expenditure -----------------------------------------------------------------


@router.get("/expenditure")
async def expenditure_api(request: Request) -> dict[str, Any]:
    """Per-pipeline AI costs + the four chart ranges, bucketed server-side from
    the same ledger the governor caps. human_* stays null until writer-pay
    baselines exist (Phase 10c) — never synthesized."""
    u = require_user(request)
    may_see_pay = u.get("role") in ("global_admin", "portfolio_admin")
    from datetime import date, datetime, timedelta, timezone

    from sqlalchemy import extract, func, select

    from .. import pricing
    from ..db.models import ContentPipeline, PipelineCost, SpendLedger, Trend
    async with RunContext.open() as ctx:
        rates = await pricing.load_rates(ctx.session)

        def usd(metric: str, amt: Any) -> float:
            return pricing.metric_to_usd(metric, amt or 0, bq_tb=rates["bq_tb"],
                                         ahrefs_unit=rates["ahrefs_unit"])

        pcs = list((await ctx.session.execute(
            select(PipelineCost).order_by(PipelineCost.completed_at.desc())
            .limit(100))).scalars().all())
        ids = []
        for r in pcs:
            rid = r.pipeline_run_id or ""
            if rid.startswith("pipeline:"):
                try:
                    ids.append(int(rid.split(":", 1)[1]))
                except ValueError:
                    pass
        titles: dict[int, str | None] = {}
        if ids:
            trows = (await ctx.session.execute(
                select(ContentPipeline.id, Trend.headline)
                .join(Trend, Trend.id == ContentPipeline.trend_id, isouter=True)
                .where(ContentPipeline.id.in_(ids)))).all()
            titles = {i: h for i, h in trows}
        rows = []
        for r in pcs:
            bd = r.cost_breakdown or {}
            rid = r.pipeline_run_id or ""
            pid = None
            if rid.startswith("pipeline:"):
                try:
                    pid = int(rid.split(":", 1)[1])
                except ValueError:
                    pid = None
            title = ((titles.get(pid) if pid is not None else None)
                     or r.article_url or rid or "—")
            rows.append({
                "id": r.id, "brand": r.brand, "title": title,
                "action_type": r.action_type or "—",
                "used_style_profile": bool(r.used_style_profile),
                "llm_usd": round(float(bd.get("llm_usd") or 0), 2),
                "ahrefs_usd": round(float(bd.get("ahrefs_usd") or 0), 2),
                "bq_usd": round(float(bd.get("bq_usd") or 0), 2),
                "other_usd": round(float(bd.get("other_usd") or 0), 2),
                "total_usd": round(float(r.total_usd or 0), 2),
                "human_equiv_usd": (round(float(r.human_equiv_usd), 2)
                                    if may_see_pay and r.human_equiv_usd is not None else None),
                "savings_usd": (round(float(r.savings_usd), 2)
                                if may_see_pay and r.savings_usd is not None else None),
                "article_url": r.article_url,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None})

        today = date.today()
        day_rows = (await ctx.session.execute(
            select(extract("hour", SpendLedger.created_at), SpendLedger.metric,
                   func.sum(SpendLedger.amount))
            .where(SpendLedger.window_date == today)
            .group_by(extract("hour", SpendLedger.created_at), SpendLedger.metric))).all()
        day_ai = [0.0] * 6
        for h, m, amt in day_rows:
            day_ai[min(5, int(h or 0) // 4)] += usd(m, amt)
        dd = (await ctx.session.execute(
            select(SpendLedger.window_date, SpendLedger.metric, func.sum(SpendLedger.amount))
            .where(SpendLedger.window_date >= today - timedelta(days=365))
            .group_by(SpendLedger.window_date, SpendLedger.metric))).all()
        daily: dict[Any, float] = {}
        for d, m, amt in dd:
            daily[d] = daily.get(d, 0.0) + usd(m, amt)
        pc_rows = (await ctx.session.execute(
            select(PipelineCost.completed_at,
                   func.coalesce(PipelineCost.human_equiv_usd, 0.0))
            .where(PipelineCost.completed_at
                   >= datetime.now(timezone.utc) - timedelta(days=366)))).all()

    WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    def human_sum(pred) -> float:
        if not may_see_pay:
            return 0.0
        return sum(float(h or 0) for t, h in pc_rows if t and pred(t))

    day = [{"label": f"{b * 4:02d}", "ai_usd": round(day_ai[b], 2),
            "human_usd": round(human_sum(
                lambda t, b=b: t.date() == today and t.hour // 4 == b), 2)}
           for b in range(6)]
    week = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        week.append({"label": WD[d.weekday()], "ai_usd": round(daily.get(d, 0.0), 2),
                     "human_usd": round(human_sum(lambda t, d=d: t.date() == d), 2)})
    month = []
    for w in range(4):
        start = today - timedelta(days=27 - w * 7)
        end = start + timedelta(days=6)
        ai = sum(v for d, v in daily.items() if start <= d <= end)
        month.append({"label": f"W{w + 1}", "ai_usd": round(ai, 2),
                      "human_usd": round(human_sum(
                          lambda t, s=start, e=end: s <= t.date() <= e), 2)})
    year = []
    yy, mm = today.year, today.month
    months = []
    for i in range(11, -1, -1):
        m2, y2 = mm - i, yy
        while m2 <= 0:
            m2 += 12
            y2 -= 1
        months.append((y2, m2))
    for (y2, m2) in months:
        ai = sum(v for d, v in daily.items() if d.year == y2 and d.month == m2)
        year.append({"label": MON[m2 - 1], "ai_usd": round(ai, 2),
                     "human_usd": round(human_sum(
                         lambda t, y2=y2, m2=m2: t.year == y2 and t.month == m2), 2)})
    return {"pipeline_costs": rows,
            "series": {"day": day, "week": week, "month": month, "year": year},
            "may_see_pay": may_see_pay}


# --- Session trends ---------------------------------------------------------------

_ST_METRIC_MAP = {"visits": "sessions", "averageEngagedDepth": "avd"}
_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _session_trend_spa(p: dict[str, Any]) -> dict[str, Any]:
    """Pivot a stored session_trends payload into the SPA's day-major shape.
    views/visits have no distinct source → null (SPA renders placeholders)."""
    from datetime import date as _date
    metrics = p.get("metrics") or {}
    visits = metrics.get("visits") or {}
    avd = metrics.get("averageEngagedDepth") or {}
    iso = p.get("iso_week") or ""
    try:
        week_no = int(str(iso).split("W")[-1])
    except ValueError:
        week_no = 0
    vser = visits.get("series") or []
    aser = avd.get("series") or []
    days = []
    for i in range(7):
        d_iso = None
        if i < len(vser):
            d_iso = (vser[i] or {}).get("date")
        if d_iso is None and i < len(aser):
            d_iso = (aser[i] or {}).get("date")
        try:
            lbl = _WD[_date.fromisoformat(d_iso).weekday()] if d_iso else _WD[i]
        except (ValueError, TypeError):
            lbl = _WD[i]
        days.append({"d": lbl, "date": d_iso,
                     "sessions": (vser[i] or {}).get("value") if i < len(vser) else None,
                     "views": None, "visits": None,
                     "avd": (aser[i] or {}).get("value") if i < len(aser) else None})
    thr = float(p.get("threshold_pct") or 25.0)

    def r1(v: Any) -> float | None:
        return round(float(v), 1) if isinstance(v, (int, float)) else None

    flags = []
    for f in (p.get("flags") or []):
        m = _ST_METRIC_MAP.get(f.get("metric"))
        if not m:
            continue          # SPA has no key for this metric — drop, don't blank
        kind = "rise" if f.get("direction") == "up" else "dip"
        note = (f"Week-over-week {kind} ≥ {thr:g}% threshold" if f.get("kind") == "wow"
                else f"Day-over-day {kind} on {f.get('date')}")
        flags.append({"metric": m, "kind": kind,
                      "delta": r1(f.get("pct")) or 0.0,
                      "date": f.get("date"), "note": note})
    return {"brand": p.get("brand"), "iso_week": week_no, "iso_week_label": iso,
            "week_start": p.get("week_start"), "threshold_pct": thr,
            "totals": {"sessions": visits.get("weekly"), "views": None, "visits": None,
                       "avd": r1(avd.get("weekly"))},
            "wow": {"sessions": r1(visits.get("wow_pct")), "views": None, "visits": None,
                    "avd": r1(avd.get("wow_pct"))},
            "series": days, "flags": flags}


@router.get("/session-trends")
async def session_trends_api(request: Request) -> dict[str, Any]:
    """Latest session-trends week per brand (same store the HTML page reads)."""
    require_user(request)
    from ..db.enums import EntryType
    settings = get_settings()
    async with RunContext.open() as ctx:
        entries = await ctx.store.query(
            types=[EntryType.METRIC], payload_contains={"kind": "session_trends"},
            fresh_within_seconds=90 * 24 * 3600, limit=300)
    latest: dict[str, dict[str, Any]] = {}
    for e in entries:
        p = e.payload or {}
        b = p.get("brand", e.brand)
        if b not in latest or (p.get("iso_week") or "") > (latest[b].get("iso_week") or ""):
            latest[b] = p
    ordered = [b for b in settings.brand_keys if b in latest]
    ordered += [b for b in latest if b not in ordered and b != "portfolio"]
    # Per-brand topic demand (§16.3): which categories pull the most sessions.
    from sqlalchemy import select as _select

    from ..db.models import BrandTopicDemand
    async with RunContext.open() as ctx:
        drows = (await ctx.session.execute(
            _select(BrandTopicDemand).order_by(BrandTopicDemand.brand, BrandTopicDemand.rank))).scalars().all()
    demand: dict[str, list[dict[str, Any]]] = {}
    for r in drows:
        demand.setdefault(r.brand, []).append({
            "category": r.category, "articles": r.articles,
            "avg_sessions": r.avg_sessions, "avg_rpm": r.avg_rpm,
            "demand_index": r.demand_index, "rank": r.rank})
    return {"trends": [_session_trend_spa(latest[b]) for b in ordered],
            "topic_demand": demand}


# --- Agent detail -----------------------------------------------------------------


@router.get("/agents/{agent_key}")
async def agent_detail_api(request: Request, agent_key: str) -> dict[str, Any]:
    """One agent: overview row (entries recomputed as a real 24h count), 7-day
    spend spark, its recent events, and pipelines it requested. tokens is
    always null — no persisted token counts exist."""
    require_user(request)
    from datetime import date, datetime, timedelta, timezone

    from sqlalchemy import func, select

    from .. import pricing
    from ..db.models import MemoryEntry, SpendLedger
    async with RunContext.open() as ctx:
        overview = await routes._agents_overview(ctx)
        row = next((a for a in overview if a.get("key") == agent_key), None)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown agent")
        now = datetime.now(timezone.utc)
        entries_24h = (await ctx.session.execute(
            select(func.count()).select_from(MemoryEntry)
            .where(MemoryEntry.source_agent == agent_key,
                   MemoryEntry.created_at >= now - timedelta(hours=24)))).scalar_one()
        rates = await pricing.load_rates(ctx.session)
        since = date.today() - timedelta(days=6)
        led = (await ctx.session.execute(
            select(SpendLedger.window_date, SpendLedger.metric, func.sum(SpendLedger.amount))
            .where(SpendLedger.agent == agent_key, SpendLedger.window_date >= since)
            .group_by(SpendLedger.window_date, SpendLedger.metric))).all()
        by_day: dict[Any, float] = {}
        for d, m, amt in led:
            by_day[d] = by_day.get(d, 0.0) + pricing.metric_to_usd(
                m, amt or 0, bq_tb=rates["bq_tb"], ahrefs_unit=rates["ahrefs_unit"])
        spark = [round(by_day.get(since + timedelta(days=i), 0.0), 2) for i in range(7)]
        raw = await routes._activity_events(ctx, 200)
        events = [e for e in raw if e.get("agent") == agent_key][:30]
        pipes = await routes.PipelineRepo(ctx.session).list(limit=80)
        pipelines = [{"id": p.id, "brand": p.brand,
                      "headline": p.trend.headline if p.trend else f"pipeline #{p.id}",
                      "status": p.status}
                     for p in pipes if p.requested_by == agent_key]
    agent = dict(row)
    agent["entries"] = int(entries_24h)
    return {"agent": agent,
            "cost": {"usd_7d": round(sum(spark), 2), "tokens": None, "spark": spark},
            "events": events, "pipelines": pipelines}


# --- Ledger (published artifacts) --------------------------------------------------


def _ledger_channel(job: Any, brand: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        handle = settings.brand(brand).domain
    except KeyError:
        handle = brand
    rr = job.result_ref or {}
    mode = rr.get("mode") or ""
    dest = {"emaki_unpublished_draft": "Emaki CMS · unpublished draft",
            "manual_handoff": "Manual hand-off"}.get(mode, mode or "Manual hand-off")
    ref = None
    url = None
    if mode == "emaki_unpublished_draft" and rr.get("topic_id") is not None:
        ref = f"topic_{rr['topic_id']}"
    aref = rr.get("artifact_ref") if isinstance(rr.get("artifact_ref"), dict) else None
    if aref is None and isinstance(job.preview_ref, dict):
        aref = job.preview_ref
    if ref is None and aref:
        ref = aref.get("key")
    if aref and aref.get("backend") == "local" and aref.get("key"):
        url = f"/api/artifact-file/{aref['key']}"
    micros = (job.cost or {}).get("llm_micros")
    pub = job.reviewed_at or job.updated_at or job.created_at
    return {"key": "web", "label": "Website", "handle": handle, "destination": dest,
            "ref": ref or "—", "url": url,
            "impressions": None, "engagements": None, "clicks": None,
            "cost": round(micros / 1e6, 2) if micros else 0.0,
            "revenue": None,
            "publishedAt": pub.isoformat() if pub else None}


def _ledger_record(job: Any, bench: dict[str, dict[str, Any]]) -> dict[str, Any]:
    brand = job.pipeline.brand if job.pipeline else "portfolio"
    meta = job.preview_meta or {}
    pub = job.reviewed_at or job.updated_at or job.created_at
    chan = _ledger_channel(job, brand)
    b = bench.get(brand, {})
    settings = get_settings()
    try:
        disp = settings.brand(brand).display_name
    except KeyError:
        disp = brand
    return {"id": _artifact_id(job.id),
            "title": _clean_title(meta.get("title"), job.content_type.replace("_", " ")),
            "brand": brand,
            "kind": _KIND_LABEL.get(job.content_type, job.content_type),
            "publishedAt": pub.isoformat() if pub else None,
            "wordCount": int(meta.get("word_count") or 0),
            "qualityScore": (meta.get("quality") or {}).get("score"),
            "channels": [chan],
            "costs": {"generation": chan["cost"], "factCheck": None,
                      "editorial": None, "distribution": None},
            "revenue": {"programmatic": None, "affiliate": None, "subscription": None},
            "benchmark": {"scope": f"{disp} · published · 30d",
                          "avgCost": b.get("avgCost"), "avgRevenue": None,
                          "avgImpressions": None, "avgEngagementRate": None,
                          "avgCtr": None, "sample": b.get("sample", 0)}}


async def _ledger_benchmarks(ctx: RunContext) -> dict[str, dict[str, Any]]:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from ..db.models import PipelineCost
    since = datetime.now(timezone.utc) - timedelta(days=30)
    rows = (await ctx.session.execute(
        select(PipelineCost.brand, func.count(), func.avg(PipelineCost.total_usd))
        .where(PipelineCost.completed_at >= since)
        .group_by(PipelineCost.brand))).all()
    return {b: {"sample": int(c), "avgCost": round(float(a), 2) if a is not None else None}
            for b, c, a in rows}


@router.get("/ledger")
async def ledger_list(request: Request, brand: str | None = None) -> dict[str, Any]:
    """Published artifacts with per-channel results. Revenue/audience metrics
    have no source yet → null (SPA renders placeholders)."""
    require_user(request)
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from ..db.models import ContentJob, ContentPipeline
    async with RunContext.open() as ctx:
        q = (select(ContentJob).options(selectinload(ContentJob.pipeline))
             .join(ContentPipeline, ContentPipeline.id == ContentJob.pipeline_id)
             .where(ContentJob.status == "published")
             .order_by(ContentJob.reviewed_at.desc().nulls_last(), ContentJob.id.desc())
             .limit(50))
        if brand:
            q = q.where(ContentPipeline.brand == brand)
        jobs = list((await ctx.session.execute(q)).scalars().all())
        bench = await _ledger_benchmarks(ctx)
        records = [_ledger_record(j, bench) for j in jobs]
    return {"records": records}


@router.get("/ledger/{record_id}")
async def ledger_record_api(request: Request, record_id: str) -> dict[str, Any]:
    require_user(request)
    jid = _parse_artifact_id(record_id)
    if jid is None:
        raise HTTPException(status_code=404, detail="ledger record not found")
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from ..db.models import ContentJob
    async with RunContext.open() as ctx:
        job = (await ctx.session.execute(
            select(ContentJob).options(selectinload(ContentJob.pipeline))
            .where(ContentJob.id == jid))).scalar_one_or_none()
        if job is None or job.status != "published":
            raise HTTPException(status_code=404, detail="ledger record not found")
        bench = await _ledger_benchmarks(ctx)
        record = _ledger_record(job, bench)
    return record


# --- Trend + pipeline detail --------------------------------------------------------


@router.get("/trends/{trend_id}")
async def trend_detail_api(request: Request, trend_id: int) -> dict[str, Any]:
    """One trend with dossier, evidence, fact-gate state, and trigger metadata."""
    require_user(request)
    from ..db.enums import EntryType
    from ..trends.lifecycle import CONTENT_TYPES
    settings = get_settings()
    async with RunContext.open() as ctx:
        trend = await routes.TrendRepo(ctx.session).get(trend_id)
        if trend is None:
            raise HTTPException(status_code=404, detail="trend not found")
        d = routes._trend_dict(trend, with_dossier=True)
        # Fact-gate state (mirrors the HTML trend-detail block).
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
        pending = [{"statement": (c.payload or {}).get("statement"), "status": c.status}
                   for c in claims
                   if (c.payload or {}).get("statement") not in set(verified)]
        d["dossier"] = trend.dossier or None      # null (not {}) so the SPA's guards work
    if not d.get("score_breakdown"):
        d["score_breakdown"] = None
    d["verified_facts"] = verified
    d["claims"] = pending
    d["semrush"] = None                            # no per-trend keyword metrics exist
    d["may_approve"] = routes._can_approve(request, d.get("brand", ""))
    return {"trend": d, "content_types": list(CONTENT_TYPES),
            "default_types": list(settings.trends.default_content_types),
            "kill_switch": settings.kill_switch}


@router.post("/trends/{trend_id}/trigger")
async def trend_trigger_api(request: Request, background_tasks: BackgroundTasks,
                            trend_id: int) -> dict[str, Any]:
    """Create (and optionally approve) a pipeline for a trend — JSON sibling of
    the HTML trigger form, same guards in the same order."""
    user = require_user(request)
    body = await _json_body(request)
    picked = body.get("content_types") or None
    instructions = (body.get("instructions") or "").strip() or None
    wants_approve = bool(body.get("approve_now"))
    persona_id = body.get("persona_id")   # None → auto-rotate at approval
    persona_id = int(persona_id) if persona_id not in (None, "", "auto") else None
    from ..trends.lifecycle import LifecycleError
    from ..trends.pipeline import approve_and_start, run_job_sweep
    async with RunContext.open() as ctx:
        trend = await routes.TrendRepo(ctx.session).get(trend_id)
        if trend is None:
            raise HTTPException(status_code=404, detail="trend not found")
        if wants_approve and not routes._can_approve(request, trend.brand):
            raise HTTPException(status_code=403, detail="not permitted for this brand")
        if trend.status not in ("detected", "dossier_building", "proposed"):
            raise HTTPException(
                status_code=400,
                detail=f"trend is {trend.status} — it can no longer be triggered")
        repo = routes.PipelineRepo(ctx.session)
        try:
            pipeline = await repo.create(
                trend_id=trend.id, brand=trend.brand,
                content_types=picked or list(get_settings().trends.default_content_types),
                requested_by=user["email"], instructions=instructions)
            trend.status = ("proposed" if trend.status in ("detected", "dossier_building")
                            else trend.status)
            pipeline_id = pipeline.id
            if wants_approve:
                await approve_and_start(ctx, pipeline_id, user["email"], persona_id=persona_id)
        except LifecycleError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    background_tasks.add_task(run_job_sweep)
    return {"ok": True, "pipeline_id": pipeline_id, "approved": wants_approve}


# Map real content-job lifecycle → the SPA's step vocabulary so the detail
# screen's status styling works untouched; the raw status rides along.
_STEP_STATUS = {"queued": "proposed", "generating": "dispatched",
                "preview_ready": "done", "approved": "done", "published": "done",
                "failed": "failed", "rejected": "rejected", "cancelled": "rejected"}


@router.get("/pipelines/{pipeline_id}")
async def pipeline_detail_api(request: Request, pipeline_id: int) -> dict[str, Any]:
    """One content pipeline with its jobs as steps. successRate/tokens are null
    — no run-history or token counts are persisted."""
    require_user(request)
    async with RunContext.open() as ctx:
        p = await routes.PipelineRepo(ctx.session).get(pipeline_id)
        if p is None:
            raise HTTPException(status_code=404, detail="pipeline not found")
        steps = []
        for job in sorted(p.jobs or [], key=lambda j: j.id or 0):
            meta = job.preview_meta or {}
            dur = 0
            if job.updated_at and job.created_at:
                dur = max(0, int((job.updated_at - job.created_at).total_seconds() * 1000))
            steps.append({"id": str(job.id),
                          "name": _clean_title(meta.get("title"),
                                               job.content_type.replace("_", " ")),
                          "agent": "production",
                          "system": meta.get("generator") or job.transport,
                          "status": _STEP_STATUS.get(job.status, "proposed"),
                          "status_raw": job.status,
                          "durationMs": dur, "tokens": None})
        out = {"id": p.id, "brand": p.brand,
               "name": p.trend.headline if p.trend else f"pipeline #{p.id}",
               "status": p.status, "ownerAgent": p.requested_by, "successRate": None,
               "may_approve": routes._can_approve(request, p.brand), "steps": steps}
    return {"pipeline": out}
