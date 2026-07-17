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
                 artifact_id: str) -> dict[str, Any]:
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
                                    kind=job.content_type, generated=generated, artifact_id=row["id"]),
            "breakdown": _breakdown_rows(breakdown),
            "signals": _artifact_signals(breakdown, scoring.fact_gate(trend)),
            "timeline": _artifact_timeline(job, row),
            "may_approve": routes._can_approve(request, brand),
        }
    return detail


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
