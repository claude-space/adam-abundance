"""Trend / content-pipeline persistence + the human-approval gate
(docs/trend-pipeline.md). Mirrors PlanRepo's posture: the scout can create
trigger requests but can never approve them; every transition is validated by
:mod:`switchboard.trends.lifecycle` and every human action lands in the
pipeline's audit timeline (``events``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db.enums import ContentJobStatus, PipelineStatus, TrendStatus
from ..db.models import ContentJob, ContentPipeline, Trend
from ..logging_ import get_logger
from .lifecycle import (
    PIPELINE_OPEN_STATUSES,
    TREND_OPEN_STATUSES,
    LifecycleError,
    require_open_pipeline,
    validate_actor,
    validate_content_types,
    validate_job_transition,
    validate_pipeline_transition,
    validate_trend_transition,
)

log = get_logger("trends.repo")

_DEDUP_SKIP_STATUSES = (TrendStatus.DISMISSED.value, TrendStatus.DECLINED.value,
                        TrendStatus.EXPIRED.value, TrendStatus.COMPLETED.value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(actor: str, event: str, detail: str | None = None) -> dict[str, Any]:
    return {"at": _now().isoformat(), "actor": actor, "event": event, "detail": detail or ""}


class TrendRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, trend_id: int) -> Trend | None:
        result = await self.session.execute(
            select(Trend).where(Trend.id == trend_id)
            .options(selectinload(Trend.pipelines).selectinload(ContentPipeline.jobs))
        )
        return result.scalar_one_or_none()

    async def list(self, *, brand: str | None = None, statuses: Sequence[str] | None = None,
                   limit: int = 50) -> list[Trend]:
        stmt = select(Trend).options(selectinload(Trend.pipelines))
        if brand:
            stmt = stmt.where(Trend.brand == brand)
        if statuses:
            stmt = stmt.where(Trend.status.in_(list(statuses)))
        stmt = stmt.order_by(Trend.score.desc(), Trend.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def find_by_cluster_key(self, cluster_key: str, *, brand: str) -> Trend | None:
        stmt = (select(Trend)
                .where(Trend.cluster_key == cluster_key, Trend.brand == brand)
                .order_by(Trend.created_at.desc()).limit(1))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self, *, brand: str, cluster_key: str, headline: str, score: float,
        score_breakdown: dict[str, Any], velocity: float, source_count: int,
        signal_count: int, covered_by_us: bool | None, entities: dict[str, Any],
        evidence: list[dict[str, Any]], ttl_hours: int, dedup_days: int,
        origin: str = "scout",
    ) -> tuple[Trend | None, bool]:
        """Create or refresh the trend for a cluster.

        Returns ``(trend, created)``; ``(None, False)`` when a recently
        dismissed/declined/expired twin suppresses re-proposal (dedup window).
        """
        existing = await self.find_by_cluster_key(cluster_key, brand=brand)
        now = _now()
        if existing is not None:
            if existing.status in _DEDUP_SKIP_STATUSES:
                seen = existing.last_seen_at or existing.created_at
                if seen is not None and (now - seen) < timedelta(days=dedup_days):
                    return None, False
            elif existing.status in TREND_OPEN_STATUSES:
                # Refresh the open trend in place.
                existing.headline = headline or existing.headline
                existing.score = max(existing.score or 0.0, score)
                existing.score_breakdown = score_breakdown
                existing.velocity = velocity
                existing.source_count = max(existing.source_count or 0, source_count)
                existing.signal_count = max(existing.signal_count or 0, signal_count)
                if covered_by_us is not None:
                    existing.covered_by_us = covered_by_us
                existing.entities = entities
                existing.evidence = (evidence or [])[:60]
                existing.last_seen_at = now
                await self.session.flush()
                return existing, False
        trend = Trend(
            brand=brand, cluster_key=cluster_key, headline=headline, score=score,
            score_breakdown=score_breakdown, velocity=velocity, source_count=source_count,
            signal_count=signal_count, covered_by_us=covered_by_us, entities=entities,
            evidence=(evidence or [])[:60], status=TrendStatus.DETECTED.value, origin=origin,
            last_seen_at=now, expires_at=now + timedelta(hours=ttl_hours),
        )
        self.session.add(trend)
        await self.session.flush()
        return trend, True

    async def set_status(self, trend_id: int, status: str) -> None:
        trend = await self.session.get(Trend, trend_id)
        if trend is None:
            return
        validate_trend_transition(trend.status, status)
        trend.status = status
        await self.session.flush()

    async def set_dossier(self, trend_id: int, dossier: dict[str, Any],
                          dossier_ref: dict[str, Any] | None) -> None:
        await self.session.execute(
            update(Trend).where(Trend.id == trend_id)
            .values(dossier=dossier, dossier_ref=dossier_ref)
        )

    async def dismiss(self, trend_id: int, actor: str) -> Trend:
        validate_actor(actor)
        trend = await self.get(trend_id)
        if trend is None:
            raise LifecycleError(f"trend {trend_id} not found")
        validate_trend_transition(trend.status, TrendStatus.DISMISSED.value)
        trend.status = TrendStatus.DISMISSED.value
        trend.last_seen_at = _now()
        await self.session.flush()
        log.info("trend %s dismissed by %s", trend_id, actor)
        return trend

    async def expire_stale(self) -> int:
        """Perishability sweep: open, unactioned trends past expires_at expire —
        along with any still-pending trigger requests they created, so dead
        requests never clog the scout's proposal cap."""
        expired_ids = (await self.session.execute(
            select(Trend.id)
            .where(Trend.status.in_([TrendStatus.DETECTED.value, TrendStatus.DOSSIER_BUILDING.value,
                                     TrendStatus.PROPOSED.value]),
                   Trend.expires_at.is_not(None), Trend.expires_at < _now())
        )).scalars().all()
        if not expired_ids:
            return 0
        await self.session.execute(
            update(Trend).where(Trend.id.in_(expired_ids))
            .values(status=TrendStatus.EXPIRED.value)
        )
        pipelines = (await self.session.execute(
            select(ContentPipeline)
            .where(ContentPipeline.trend_id.in_(expired_ids),
                   ContentPipeline.status == PipelineStatus.PENDING_APPROVAL.value)
        )).scalars().all()
        for p in pipelines:
            p.status = PipelineStatus.EXPIRED.value
            p.closed_at = _now()
            p.close_reason = "trend expired before a decision"
            p.events = (p.events or []) + [_event("system", "expired", "trend expired")]
        await self.session.flush()
        log.info("expired %d stale trends (+%d pending trigger requests)",
                 len(expired_ids), len(pipelines))
        return len(expired_ids)

    async def counts_by_status(self) -> dict[str, int]:
        rows = (await self.session.execute(
            text("SELECT status, COUNT(*) FROM trend GROUP BY status")
        )).all()
        return {status: int(n) for status, n in rows}


class PipelineRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- reads -----------------------------------------------------------------

    async def get(self, pipeline_id: int) -> ContentPipeline | None:
        result = await self.session.execute(
            select(ContentPipeline).where(ContentPipeline.id == pipeline_id)
            .options(selectinload(ContentPipeline.jobs),
                     # chain Trend.pipelines: _trend_dict reads it, and an async
                     # lazy-load would raise MissingGreenlet
                     selectinload(ContentPipeline.trend).selectinload(Trend.pipelines))
        )
        return result.scalar_one_or_none()

    async def list(self, *, brand: str | None = None, statuses: Sequence[str] | None = None,
                   limit: int = 50) -> list[ContentPipeline]:
        stmt = select(ContentPipeline).options(
            selectinload(ContentPipeline.jobs), selectinload(ContentPipeline.trend)
        )
        if brand:
            stmt = stmt.where(ContentPipeline.brand == brand)
        if statuses:
            stmt = stmt.where(ContentPipeline.status.in_(list(statuses)))
        stmt = stmt.order_by(ContentPipeline.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def open_count(self, brand: str) -> int:
        row = await self.session.execute(
            select(ContentPipeline.id)
            .where(ContentPipeline.brand == brand,
                   ContentPipeline.status.in_(list(PIPELINE_OPEN_STATUSES)))
        )
        return len(row.scalars().all())

    async def get_job(self, job_id: int) -> ContentJob | None:
        result = await self.session.execute(
            select(ContentJob).where(ContentJob.id == job_id)
            .options(selectinload(ContentJob.pipeline).selectinload(ContentPipeline.trend))
        )
        return result.scalar_one_or_none()

    # -- lifecycle: pipeline ---------------------------------------------------

    def _log_event(self, pipeline: ContentPipeline, actor: str, event: str,
                   detail: str | None = None) -> None:
        pipeline.events = (pipeline.events or []) + [_event(actor, event, detail)]

    async def create(self, *, trend_id: int | None, brand: str, content_types: list[str],
                     requested_by: str = "trend_scout", instructions: str | None = None) -> ContentPipeline:
        if trend_id is not None:
            dup = (await self.session.execute(
                select(ContentPipeline.id)
                .where(ContentPipeline.trend_id == trend_id,
                       ContentPipeline.status.in_(list(PIPELINE_OPEN_STATUSES)))
                .limit(1)
            )).scalar_one_or_none()
            if dup is not None:
                raise LifecycleError(f"trend already has an open pipeline (#{dup})")
        pipeline = ContentPipeline(
            trend_id=trend_id, brand=brand, status=PipelineStatus.PENDING_APPROVAL.value,
            requested_by=requested_by, instructions=instructions,
            content_types=validate_content_types(content_types),
        )
        self._log_event(pipeline, requested_by, "created", f"content_types={content_types}")
        self.session.add(pipeline)
        await self.session.flush()
        return pipeline

    async def approve(self, pipeline_id: int, approver: str, *,
                      content_types: list[str] | None = None,
                      instructions: str | None = None) -> ContentPipeline:
        validate_actor(approver)
        pipeline = await self.get(pipeline_id)
        if pipeline is None:
            raise LifecycleError(f"pipeline {pipeline_id} not found")
        validate_pipeline_transition(pipeline.status, PipelineStatus.APPROVED.value)
        pipeline.status = PipelineStatus.APPROVED.value
        pipeline.approved_by = approver
        pipeline.approved_at = _now()
        if content_types:
            pipeline.content_types = validate_content_types(content_types)
        if instructions is not None and instructions.strip():
            pipeline.instructions = instructions.strip()
        self._log_event(pipeline, approver, "approved",
                        f"content_types={pipeline.content_types}")
        await self.session.flush()
        log.info("pipeline %s approved by %s", pipeline_id, approver)
        return pipeline

    async def decline(self, pipeline_id: int, actor: str, reason: str | None = None) -> ContentPipeline:
        validate_actor(actor)
        pipeline = await self.get(pipeline_id)
        if pipeline is None:
            raise LifecycleError(f"pipeline {pipeline_id} not found")
        validate_pipeline_transition(pipeline.status, PipelineStatus.DECLINED.value)
        pipeline.status = PipelineStatus.DECLINED.value
        pipeline.declined_by = actor
        pipeline.declined_at = _now()
        pipeline.close_reason = reason
        self._log_event(pipeline, actor, "declined", reason)
        await self.session.flush()
        log.info("pipeline %s declined by %s", pipeline_id, actor)
        return pipeline

    async def close(self, pipeline_id: int, actor: str, reason: str | None = None) -> ContentPipeline:
        validate_actor(actor)
        pipeline = await self.get(pipeline_id)
        if pipeline is None:
            raise LifecycleError(f"pipeline {pipeline_id} not found")
        validate_pipeline_transition(pipeline.status, PipelineStatus.CLOSED.value)
        pipeline.status = PipelineStatus.CLOSED.value
        pipeline.closed_at = _now()
        pipeline.close_reason = reason
        # Cancel every non-terminal job — a closed pipeline must not keep live,
        # actionable jobs around (only published stays as the record of what shipped).
        for job in pipeline.jobs:
            if job.status not in (ContentJobStatus.PUBLISHED.value,
                                  ContentJobStatus.CANCELLED.value):
                job.status = ContentJobStatus.CANCELLED.value
                job.updated_at = _now()
        self._log_event(pipeline, actor, "closed", reason)
        await self.session.flush()
        log.info("pipeline %s closed by %s", pipeline_id, actor)
        return pipeline

    async def set_status(self, pipeline_id: int, status: str, *, actor: str = "pipeline",
                         detail: str | None = None) -> None:
        pipeline = await self.get(pipeline_id)
        if pipeline is None:
            return
        validate_pipeline_transition(pipeline.status, status)
        pipeline.status = status
        self._log_event(pipeline, actor, status, detail)
        await self.session.flush()

    async def refresh_rollup(self, pipeline_id: int) -> str:
        """Derive the pipeline status from its jobs after a job event."""
        pipeline = await self.get(pipeline_id)
        if pipeline is None:
            return ""
        statuses = [j.status for j in pipeline.jobs]
        if not statuses or pipeline.status in (PipelineStatus.DECLINED.value,
                                               PipelineStatus.CLOSED.value,
                                               PipelineStatus.EXPIRED.value):
            return pipeline.status
        live = ContentJobStatus
        new = pipeline.status
        if any(s in (live.QUEUED.value, live.RUNNING.value) for s in statuses):
            new = PipelineStatus.GENERATING.value
        elif all(s in (live.PUBLISHED.value, live.REJECTED.value, live.CANCELLED.value)
                 for s in statuses):
            published = sum(1 for s in statuses if s == live.PUBLISHED.value)
            if published and published == len([s for s in statuses if s != live.CANCELLED.value]):
                new = PipelineStatus.PUBLISHED.value
            elif published:
                new = PipelineStatus.PARTIALLY_PUBLISHED.value
            else:
                new = PipelineStatus.CLOSED.value
        elif all(s == live.FAILED.value for s in statuses):
            new = PipelineStatus.FAILED.value
        elif any(s in (live.PREVIEW_READY.value, live.APPROVED.value, live.PUBLISHED.value)
                 for s in statuses):
            new = PipelineStatus.PREVIEWS_READY.value
        if new != pipeline.status:
            try:
                validate_pipeline_transition(pipeline.status, new)
            except LifecycleError:
                return pipeline.status
            pipeline.status = new
            if new == PipelineStatus.CLOSED.value:
                pipeline.closed_at = _now()
                pipeline.close_reason = pipeline.close_reason or "all previews rejected"
            self._log_event(pipeline, "pipeline", new)
            await self.session.flush()
        return pipeline.status

    # -- lifecycle: jobs ---------------------------------------------------------

    async def add_job(self, pipeline: ContentPipeline, *, content_type: str, transport: str,
                      instructions: str | None = None) -> ContentJob:
        job = ContentJob(
            pipeline_id=pipeline.id, content_type=content_type, transport=transport,
            status=ContentJobStatus.QUEUED.value, instructions=instructions,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def claim_queued(self, limit: int = 5) -> list[int]:
        """Atomically claim queued jobs (queued → running); safe across the web
        and scheduler processes via SKIP LOCKED."""
        rows = (await self.session.execute(text(
            "UPDATE content_job SET status = 'running', updated_at = now() "
            "WHERE id IN (SELECT id FROM content_job WHERE status = 'queued' "
            "ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED) RETURNING id"
        ), {"n": limit})).scalars().all()
        return [int(r) for r in rows]

    async def claim_stuck_running(self, cutoff: datetime, limit: int = 5) -> list[int]:
        """Atomically claim jobs stuck waiting on an external pipeline (bumping
        updated_at debounces other workers for the next poll window)."""
        if limit <= 0:
            return []
        rows = (await self.session.execute(text(
            "UPDATE content_job SET updated_at = now() "
            "WHERE id IN (SELECT id FROM content_job WHERE status = 'running' "
            "AND external_ref IS NOT NULL AND updated_at < :cutoff "
            "ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED) RETURNING id"
        ), {"cutoff": cutoff, "n": limit})).scalars().all()
        return [int(r) for r in rows]

    async def reap_dead_running(self, cutoff: datetime) -> int:
        """A worker that died mid-generation leaves a 'running' job with no
        external pipeline to resume — fail it so the editor can regenerate."""
        rows = (await self.session.execute(text(
            "UPDATE content_job SET status = 'failed', updated_at = now(), "
            "error = 'worker died mid-generation — regenerate to retry' "
            "WHERE id IN (SELECT id FROM content_job WHERE status = 'running' "
            "AND external_ref IS NULL AND updated_at < :cutoff "
            "FOR UPDATE SKIP LOCKED) RETURNING pipeline_id"
        ), {"cutoff": cutoff})).scalars().all()
        if rows:
            log.warning("reaped %d dead running job(s)", len(rows))
            for pipeline_id in dict.fromkeys(int(p) for p in rows if p is not None):
                await self.refresh_rollup(pipeline_id)
        return len(rows)

    async def mark_job(self, job_id: int, status: str, **fields: Any) -> ContentJob:
        job = await self.get_job(job_id)
        if job is None:
            raise LifecycleError(f"content_job {job_id} not found")
        validate_job_transition(job.status, status)
        job.status = status
        job.updated_at = _now()
        for key, value in fields.items():
            setattr(job, key, value)
        await self.session.flush()
        return job

    async def review_job(self, job_id: int, actor: str, *, approve: bool) -> ContentJob:
        """Editor verdict on a preview: approve (ready to publish) or reject."""
        validate_actor(actor)
        job = await self.get_job(job_id)
        if job is None:
            raise LifecycleError(f"content_job {job_id} not found")
        if job.pipeline is not None:
            require_open_pipeline(job.pipeline.status)  # closed pipelines stay closed
        status = ContentJobStatus.APPROVED.value if approve else ContentJobStatus.REJECTED.value
        job = await self.mark_job(job_id, status, reviewed_by=actor, reviewed_at=_now())
        if job.pipeline is not None:
            self._log_event(job.pipeline, actor,
                            "preview_approved" if approve else "preview_rejected",
                            f"job {job_id} ({job.content_type})")
            await self.session.flush()
        return job

    async def regenerate_job(self, job_id: int, actor: str, instructions: str) -> ContentJob:
        """Archive the current attempt and re-queue with the editor's guidance."""
        validate_actor(actor)
        job = await self.get_job(job_id)
        if job is None:
            raise LifecycleError(f"content_job {job_id} not found")
        if job.pipeline is not None:
            require_open_pipeline(job.pipeline.status)  # closed pipelines stay closed
        validate_job_transition(job.status, ContentJobStatus.QUEUED.value)
        job.history = (job.history or []) + [{
            "attempt": job.attempt, "instructions": job.instructions,
            "preview_ref": job.preview_ref, "preview_meta": job.preview_meta,
            "at": _now().isoformat(),
        }]
        job.attempt += 1
        job.instructions = instructions.strip() or job.instructions
        job.status = ContentJobStatus.QUEUED.value
        job.preview_ref = None
        job.preview_meta = None
        job.error = None
        job.updated_at = _now()
        if job.pipeline is not None:
            self._log_event(job.pipeline, actor, "regenerate_requested",
                            f"job {job_id} attempt {job.attempt}")
        await self.session.flush()
        log.info("content_job %s re-queued (attempt %d) by %s", job_id, job.attempt, actor)
        return job
