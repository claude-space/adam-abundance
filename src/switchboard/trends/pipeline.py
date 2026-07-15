"""The content-pipeline engine (docs/trend-pipeline.md): after a human approves
a trigger request, queue one job per content type, generate previews, and gate
the final publish/hand-off behind a second explicit human action.

Jobs are DB-queued: the web process fast-paths them with FastAPI BackgroundTasks
and the scheduler's ``pipeline_jobs`` sweep is the cross-process fallback —
``PipelineRepo.claim_queued`` (SKIP LOCKED) makes double-processing impossible.

Safety rails, same as dispatch (PRD §8): the kill switch refuses generation and
publishing; LLM spend is pre-checked + charged by LLMClient; publishing means
the sanctioned Emaki *unpublished draft* push or an explicit manual hand-off —
never an autonomous post.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..artifacts import ArtifactStore
from ..context import RunContext
from ..db.enums import ContentJobStatus, EntryType, PipelineStatus, TrendStatus
from ..db.models import ContentJob, ContentPipeline
from ..interfaces import EntryDraft
from ..logging_ import get_logger
from ..orchestrator.slack import notify_trend_event
from .generators import generate
from .lifecycle import (
    TREND_OPEN_STATUSES,
    LifecycleError,
    require_recoverable_pipeline,
    validate_actor,
)
from .repo import PipelineRepo, TrendRepo

log = get_logger("trends.pipeline")

_STUCK_AFTER = timedelta(minutes=5)   # re-poll pending external jobs after this
_DEAD_AFTER = timedelta(minutes=30)   # a running job with no external ref died with its worker


# -- approval → jobs ---------------------------------------------------------------

async def approve_and_start(ctx: RunContext, pipeline_id: int, approver: str, *,
                            content_types: list[str] | None = None,
                            instructions: str | None = None) -> ContentPipeline:
    """Record the human approval, queue one job per content type, and notify.
    Generation itself happens in the job worker (fast-path or sweep)."""
    repo = PipelineRepo(ctx.session)
    pipeline = await repo.get(pipeline_id)
    if pipeline is None:
        raise LifecycleError(f"pipeline {pipeline_id} not found")
    if pipeline.trend is not None and pipeline.trend.status not in TREND_OPEN_STATUSES:
        raise LifecycleError(
            f"trend is {pipeline.trend.status!r} — decline or close this request instead")
    # Production suppression gate (§16.2): refuse to start content for a fading
    # trend. Soft + overridable — lift suppression on the trend to proceed.
    if pipeline.trend is not None and getattr(pipeline.trend, "suppressed", False):
        raise LifecycleError(
            f"trend is suppressed (state={pipeline.trend.state!r}) — its activity is fading. "
            "Lift suppression on the trend to override, then approve.")
    pipeline = await repo.approve(pipeline_id, approver,
                                  content_types=content_types, instructions=instructions)
    transports = ctx.settings.trends
    for content_type in pipeline.content_types or []:
        await repo.add_job(pipeline, content_type=content_type,
                           transport=transports.transport_for(content_type))
    await repo.set_status(pipeline.id, PipelineStatus.GENERATING.value,
                          actor=approver, detail="jobs queued")
    if pipeline.trend_id is not None:
        try:
            await TrendRepo(ctx.session).set_status(pipeline.trend_id, "approved")
        except LifecycleError as exc:  # already approved via another path — not fatal
            log.info("[pipeline] trend status unchanged on approve: %s", exc)
    headline = pipeline.trend.headline if pipeline.trend else f"pipeline #{pipeline.id}"
    await notify_trend_event(ctx, pipeline.brand, "pipeline_approved",
                             headline=headline, trend_id=pipeline.trend_id,
                             pipeline_id=pipeline.id)
    return pipeline


async def decline_pipeline(ctx: RunContext, pipeline_id: int, actor: str,
                           reason: str | None = None) -> ContentPipeline:
    repo = PipelineRepo(ctx.session)
    pipeline = await repo.decline(pipeline_id, actor, reason)
    # Only a still-proposed trend follows its request into 'declined'; a trend
    # with other live/published work keeps its own status.
    if (pipeline.trend_id is not None and pipeline.trend is not None
            and pipeline.trend.status == TrendStatus.PROPOSED.value):
        await TrendRepo(ctx.session).set_status(pipeline.trend_id, "declined")
    headline = pipeline.trend.headline if pipeline.trend else f"pipeline #{pipeline.id}"
    await notify_trend_event(ctx, pipeline.brand, "pipeline_declined",
                             headline=headline, trend_id=pipeline.trend_id,
                             pipeline_id=pipeline.id, detail=reason)
    return pipeline


# -- the job worker ----------------------------------------------------------------

async def run_job_sweep(limit: int = 5) -> dict[str, int]:
    """Process queued jobs + resume pending external ones. Called by the
    scheduler every couple of minutes and by the web fast-path right after
    approval/regeneration.

    Transactional shape matters here (external calls are not rollback-able):
    the *claim* commits immediately in its own transaction, then every job runs
    in its own transaction — one job's failure can never roll back another
    job's completed work, and a crash leaves claimed jobs in 'running' for the
    stale-job reaper rather than silently re-queuing side-effectful work."""
    async with RunContext.open() as ctx:  # short claim transaction, commits at exit
        repo = PipelineRepo(ctx.session)
        job_ids = await repo.claim_queued(limit)
        now = datetime.now(timezone.utc)
        job_ids += await repo.claim_stuck_running(now - _STUCK_AFTER,
                                                  limit=max(0, limit - len(job_ids)))
        await repo.reap_dead_running(now - _DEAD_AFTER)

    done = failed = pending = 0
    for job_id in job_ids:
        try:
            async with RunContext.open() as job_ctx:  # one transaction per job
                outcome = await _run_job(job_ctx, job_id)
        except Exception as exc:  # noqa: BLE001 — isolate: one bad job must not stop the sweep
            log.exception("[pipeline] job %s crashed: %s", job_id, exc)
            outcome = "failed"
            await _mark_failed_safely(job_id, f"worker error: {exc}")
        if outcome == "ok":
            done += 1
        elif outcome == "pending":
            pending += 1
        else:
            failed += 1
    if done or failed or pending:
        log.info("[pipeline] job sweep: ok=%d pending=%d failed=%d", done, pending, failed)
    return {"ok": done, "pending": pending, "failed": failed}


async def _mark_failed_safely(job_id: int, error: str) -> None:
    """Record a crash outcome in a fresh transaction (the job's own rolled back)."""
    try:
        async with RunContext.open() as ctx:
            repo = PipelineRepo(ctx.session)
            job = await repo.mark_job(job_id, ContentJobStatus.FAILED.value, error=error[:500])
            if job.pipeline_id is not None:
                await repo.refresh_rollup(job.pipeline_id)
    except Exception as exc:  # noqa: BLE001 — last resort: leave it to the stale reaper
        log.warning("[pipeline] could not mark job %s failed: %s", job_id, exc)


async def _run_job(ctx: RunContext, job_id: int) -> str:
    repo = PipelineRepo(ctx.session)
    job = await repo.get_job(job_id)
    if job is None or job.pipeline is None:
        return "failed"
    pipeline = job.pipeline
    trend = pipeline.trend

    if ctx.settings.kill_switch:
        await repo.mark_job(job_id, ContentJobStatus.FAILED.value,
                            error="kill switch is ON — generation refused (PRD §8)")
        await repo.refresh_rollup(pipeline.id)
        return "failed"
    if trend is None:
        await repo.mark_job(job_id, ContentJobStatus.FAILED.value,
                            error="trend record missing")
        await repo.refresh_rollup(pipeline.id)
        return "failed"

    result = await generate(ctx, job, pipeline, trend)

    if result.pending:
        job.external_ref = result.external_ref or job.external_ref
        job.updated_at = datetime.now(timezone.utc)
        await ctx.session.flush()
        return "pending"

    if not result.ok:
        await repo.mark_job(job_id, ContentJobStatus.FAILED.value,
                            error=result.error or "generation failed",
                            external_ref=result.external_ref or job.external_ref)
        await repo.refresh_rollup(pipeline.id)
        return "failed"

    preview_ref = None
    try:
        preview_ref = ArtifactStore().put_text(
            brand=pipeline.brand, kind=f"trend_{job.content_type}", ext="md",
            text=result.preview_markdown, content_type="text/markdown",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[pipeline] preview artifact failed for job %s: %s", job_id, exc)

    cost = {"llm_micros": result.cost_micros} if result.cost_micros else None
    before = pipeline.status
    await repo.mark_job(
        job_id, ContentJobStatus.PREVIEW_READY.value,
        preview_ref=preview_ref, preview_meta=result.preview_meta,
        external_ref=result.external_ref or job.external_ref, cost=cost, error=None,
    )
    await _write_preview_entry(ctx, pipeline, job, preview_ref, result.preview_meta)
    after = await repo.refresh_rollup(pipeline.id)
    if after == PipelineStatus.PREVIEWS_READY.value and before != after:
        headline = trend.headline if trend else f"pipeline #{pipeline.id}"
        await notify_trend_event(ctx, pipeline.brand, "previews_ready",
                                 headline=headline, trend_id=pipeline.trend_id,
                                 pipeline_id=pipeline.id)
    return "ok"


async def _write_preview_entry(ctx: RunContext, pipeline: ContentPipeline, job: ContentJob,
                               preview_ref: dict[str, Any] | None,
                               meta: dict[str, Any]) -> None:
    """Previews also land in shared memory, so /distribution and /memory see them."""
    await ctx.store.write(EntryDraft(
        type=EntryType.DISTRIBUTION_DRAFT, brand=pipeline.brand,
        source_agent="trend_pipeline", source_system="trend_pipeline",
        payload={"kind": "trend_content_draft", "pipeline_id": pipeline.id, "job_id": job.id,
                 "content_type": job.content_type, "attempt": job.attempt,
                 "title": meta.get("title", ""), "status": "preview_ready",
                 **({"artifact_ref": preview_ref} if preview_ref else {})},
    ))


# -- publish gate ------------------------------------------------------------------

async def publish_job(ctx: RunContext, job_id: int, actor: str) -> ContentJob:
    """The second human gate. 'Publish' never means going live autonomously:
    for the hc_viral transport it pushes the sanctioned *unpublished* Emaki CMS
    draft; for everything else it records an explicit manual hand-off of the
    approved artifact. Refused outright while the kill switch is on."""
    validate_actor(actor)
    if ctx.settings.kill_switch:
        raise LifecycleError("kill switch is ON — publish refused (PRD §8)")
    repo = PipelineRepo(ctx.session)
    job = await repo.get_job(job_id)
    if job is None or job.pipeline is None:
        raise LifecycleError(f"content_job {job_id} not found")
    require_recoverable_pipeline(job.pipeline.status)  # closed/declined pipelines stay closed
    if job.status != ContentJobStatus.APPROVED.value:
        raise LifecycleError("approve the preview before publishing")
    pipeline = job.pipeline

    if job.transport == "hc_viral_hits" and (job.external_ref or {}).get("topic_id"):
        result_ref = await _emaki_push(ctx, pipeline.brand, job)
    else:
        result_ref = {"mode": "manual_handoff", "artifact_ref": job.preview_ref,
                      "note": "approved for hand-off; a human performs the actual publish/post"}

    job = await repo.mark_job(job_id, ContentJobStatus.PUBLISHED.value,
                              result_ref=result_ref, reviewed_by=actor,
                              reviewed_at=datetime.now(timezone.utc))
    await ctx.store.write(EntryDraft(
        type=EntryType.DECISION, brand=pipeline.brand, source_agent="trend_pipeline",
        source_system="trend_pipeline",
        payload={"kind": "trend_content_published", "pipeline_id": pipeline.id,
                 "job_id": job.id, "content_type": job.content_type,
                 "approved_by": actor, "result": result_ref},
    ))
    await repo.refresh_rollup(pipeline.id)
    if pipeline.status in (PipelineStatus.PUBLISHED.value,
                           PipelineStatus.PARTIALLY_PUBLISHED.value):
        if pipeline.trend_id is not None:
            await TrendRepo(ctx.session).set_status(pipeline.trend_id, "completed")
        # Roll this pipeline's metered LLM spend up to one PipelineCost row in USD
        # (§16.4) so the Expenditure AI-vs-human panel can read it. Best-effort:
        # a rollup failure must never block a completed publish.
        try:
            fresh = await repo.get(pipeline.id)   # reload so .jobs is populated
            if fresh is not None:
                await _record_pipeline_cost(ctx, fresh)
        except Exception as exc:  # noqa: BLE001
            log.warning("[pipeline] pipeline_cost rollup failed for %s: %s", pipeline.id, exc)
    headline = pipeline.trend.headline if pipeline.trend else f"pipeline #{pipeline.id}"
    await notify_trend_event(ctx, pipeline.brand, "content_published",
                             headline=headline, trend_id=pipeline.trend_id,
                             pipeline_id=pipeline.id,
                             detail=f"{job.content_type} via {job.transport}")
    return job


async def _emaki_push(ctx: RunContext, brand: str, job: ContentJob) -> dict[str, Any]:
    """The one sanctioned CMS action: push the finished hc-viral topic to Emaki
    as an UNPUBLISHED draft (same rule as the emaki_publish_draft plan action)."""
    try:
        import httpx  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise LifecycleError("httpx not installed") from exc
    base = (ctx.settings.endpoints.get("hc_viral_hits") or "").rstrip("/")
    email = ctx.creds.resolve("HC_VIRAL_HITS_LOGIN_EMAIL", secret=False)
    password = ctx.creds.resolve("HC_VIRAL_HITS_LOGIN_PASSWORD")
    topic_id = (job.external_ref or {}).get("topic_id")
    if not (base and email and password and topic_id):
        raise LifecycleError("hc-viral session credentials/topic missing for Emaki push")
    async with httpx.AsyncClient(base_url=base, timeout=60.0) as client:
        resp = await client.post("/api/auth/login", json={"email": email, "password": password})
        resp.raise_for_status()
        # Brand-scoped: pushing under the session's default brand would target
        # the wrong CMS — treat a failed brand switch as fatal.
        brand_resp = await client.post("/api/auth/active-brand", json={"brand_slug": brand})
        if brand_resp.status_code >= 400:
            raise LifecycleError(
                f"hc-viral active-brand '{brand}' failed ({brand_resp.status_code}) — refusing "
                "to push under the wrong brand")
        resp = await client.post(f"/api/topics/{topic_id}/emaki-publish", json={})
        resp.raise_for_status()
        accepted = resp.json()
    await ctx.store.log_tool_call(
        agent="trend_pipeline", tool="emaki_publish", action="act", dry_run=False,
        brand=brand, request={"topic_id": topic_id}, ok=True,
    )
    return {"mode": "emaki_unpublished_draft", "topic_id": topic_id, "accepted": accepted}


# -- cost rollup (§16.4) -----------------------------------------------------------

async def _record_pipeline_cost(ctx: RunContext, pipeline: ContentPipeline) -> None:
    """Upsert one PipelineCost row summing this pipeline's metered LLM spend in
    USD. Idempotent per pipeline (a PARTIALLY_PUBLISHED → PUBLISHED transition
    just refreshes the same row). The human-equivalent is filled ONLY when a
    ``writer_pay_baseline`` rate exists for the brand — until Andrew provides
    rates it stays NULL, and the Expenditure panel shows its awaiting state
    rather than a fabricated saving."""
    from sqlalchemy import select

    from .. import pricing
    from ..db.models import PipelineCost

    micros = 0
    words = 0
    used_profile = False
    style_profile_id: int | None = None
    for job in pipeline.jobs:
        if job.cost:
            micros += int(job.cost.get("llm_micros") or 0)
        meta = job.preview_meta or {}
        words += int(meta.get("word_count") or 0)
        # Stamped by the generator (§16.3) when a house style profile was folded
        # into the draft; false for every job until a profile has been distilled.
        if meta.get("used_style_profile"):
            used_profile = True
            style_profile_id = style_profile_id or meta.get("style_profile_id")

    llm_usd = pricing.metric_to_usd("llm_micros", micros)
    total_usd = round(llm_usd, 6)
    # The pipeline itself only spends LLM (research/ahrefs live upstream in the
    # scout and are not attributable to this publish), so the other lanes are 0.
    breakdown = {"llm_usd": total_usd, "ahrefs_usd": 0.0, "bq_usd": 0.0, "other_usd": 0.0}

    human_equiv = await _human_equiv_usd(ctx.session, pipeline.brand, words)
    savings = round(human_equiv - total_usd, 2) if human_equiv is not None else None

    run_id = f"pipeline:{pipeline.id}"
    existing = (await ctx.session.execute(
        select(PipelineCost).where(PipelineCost.pipeline_run_id == run_id)
    )).scalar_one_or_none()
    if existing is None:
        ctx.session.add(PipelineCost(
            pipeline_run_id=run_id, brand=pipeline.brand, article_url=None,
            action_type="trend_pipeline", used_style_profile=used_profile,
            style_profile_id=style_profile_id, cost_breakdown=breakdown, total_usd=total_usd,
            human_equiv_usd=human_equiv, savings_usd=savings,
        ))
    else:
        existing.cost_breakdown = breakdown
        existing.total_usd = total_usd
        existing.human_equiv_usd = human_equiv
        existing.savings_usd = savings
        existing.used_style_profile = used_profile
        existing.style_profile_id = style_profile_id
        existing.completed_at = datetime.now(timezone.utc)
    await ctx.session.flush()


async def _human_equiv_usd(session: Any, brand: str, words: int) -> float | None:
    """The brand's human-writer pay baseline for one article, or None when no
    rate is configured. Prefers a flat per-article rate; falls back to
    per-word × the article's word count."""
    from sqlalchemy import select

    from ..db.models import WriterPayBaseline

    row = (await session.execute(
        select(WriterPayBaseline)
        .where(WriterPayBaseline.brand == brand, WriterPayBaseline.author.is_(None))
        .order_by(WriterPayBaseline.effective_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if row is None:
        return None
    if row.usd_per_article is not None:
        return round(float(row.usd_per_article), 2)
    if row.usd_per_word is not None and words:
        return round(float(row.usd_per_word) * words, 2)
    return None
