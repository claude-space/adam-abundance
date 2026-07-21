"""Engine tests for :mod:`switchboard.trends.pipeline` — the content-pipeline
state machine: approve → queue jobs → generate → preview → publish/decline, plus
the cost rollup, the job sweep, and the safety rails (kill switch, actor gate,
terminal-pipeline guards).

DB-backed: these run against the Postgres in ``DATABASE_URL`` and self-skip when
none is reachable. Everything with a side effect outside the DB is mocked at the
pipeline module's own seams — ``generate`` (LLM/transports), ``ArtifactStore``
(blob store), ``notify_trend_event`` (Slack), and ``_emaki_push`` (CMS push) — so
no network and no real content generation ever happens.

Isolation: every row a test writes is marked (``requested_by``/``cluster_key``/a
synthetic brand) and scrubbed by an autouse fixture before and after each test,
so the shared DB is never polluted.  Distinct from ``tests/integration/
test_pipeline.py`` (dispatch) and ``tests/integration/test_pipeline_cost.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from switchboard.context import RunContext
from switchboard.db.enums import ContentJobStatus, EntryType, PipelineStatus
from switchboard.db.models import (
    ContentJob,
    ContentPipeline,
    MemoryEntry,
    PipelineCost,
    Trend,
    WriterPayBaseline,
    WriterStyleProfile,
)
from switchboard.trends import personas as personas_mod
from switchboard.trends import pipeline as pipe
from switchboard.trends.generators import GenerationResult
from switchboard.trends.lifecycle import LifecycleError
from switchboard.trends.repo import PipelineRepo, TrendRepo

USER = "andrew.marks@valnetinc.com"
MARK = "itest_pipeng"            # requested_by marker on every pipeline we create
CLUSTER_PREFIX = "itest-pipeng-"  # cluster_key prefix on every trend we create
SYNTH_BRAND = "itest_pipeng"      # synthetic brand for cost-only tests (no store.write)
REAL_BRAND = "hotcars"            # a valid scope, required whenever store.write runs


# --------------------------------------------------------------------------- #
# fixtures: DB skip + marker-keyed cleanup + no-Slack
# --------------------------------------------------------------------------- #

async def _scrub() -> None:
    """Delete every row this module can create. Keyed off the markers above so it
    never touches real data. Best-effort: a no-op when Postgres is unreachable."""
    try:
        async with RunContext.open() as c:
            s = c.session
            # Orphan jobs (pipeline_id NULL) escape the pipeline cascade, so delete
            # every job we tagged with the marker first.
            await s.execute(delete(ContentJob).where(ContentJob.instructions == MARK))
            pids = (await s.execute(
                select(ContentPipeline.id).where(ContentPipeline.requested_by == MARK)
            )).scalars().all()
            for pid in pids:
                await s.execute(delete(MemoryEntry).where(
                    MemoryEntry.source_system == "trend_pipeline",
                    MemoryEntry.payload["pipeline_id"].astext == str(pid)))
            if pids:
                run_ids = [f"pipeline:{p}" for p in pids]
                await s.execute(delete(PipelineCost).where(PipelineCost.pipeline_run_id.in_(run_ids)))
                # content_job rows go with the pipeline (FK ON DELETE CASCADE).
                await s.execute(delete(ContentPipeline).where(ContentPipeline.id.in_(pids)))
            await s.execute(delete(Trend).where(Trend.cluster_key.like(CLUSTER_PREFIX + "%")))
            await s.execute(delete(PipelineCost).where(PipelineCost.brand == SYNTH_BRAND))
            await s.execute(delete(WriterPayBaseline).where(WriterPayBaseline.brand == SYNTH_BRAND))
            await s.execute(delete(WriterStyleProfile).where(WriterStyleProfile.brand == SYNTH_BRAND))
            await s.commit()
    except Exception:  # noqa: BLE001 — no DB / nothing to scrub
        pass


@pytest.fixture(autouse=True)
async def _cleanup_rows():
    await _scrub()
    yield
    await _scrub()


@pytest.fixture(autouse=True)
def _no_slack(monkeypatch):
    """Never hit Slack; hand tests the mock so they can assert notifications."""
    m = AsyncMock(return_value=False)
    monkeypatch.setattr(pipe, "notify_trend_event", m)
    return m


@pytest.fixture
async def db_ctx():
    """A live RunContext bound to DATABASE_URL; skips when no Postgres is up.

    Direct-call tests seed + act + assert inside this one transaction."""
    try:
        async with RunContext.open() as c:
            await c.store.expire_stale()  # connectivity ping
            yield c
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no reachable Postgres: {exc}")


@pytest.fixture
async def require_db():
    """Skip guard for tests that open their own (committed) contexts."""
    try:
        async with RunContext.open() as c:
            await c.store.expire_stale()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no reachable Postgres: {exc}")
    yield


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

_EVIDENCE = [{"url": "https://example.com/a", "source": "Example", "title": "A"}]


def _gen(result: GenerationResult):
    """A stand-in for pipeline.generate that returns a fixed result."""
    async def _g(ctx, job, pipeline, trend):
        return result
    return _g


def _gen_raises(exc: Exception):
    async def _g(ctx, job, pipeline, trend):
        raise exc
    return _g


def _pick(ret):
    async def _p(session, brand, persona_id=None):
        return ret
    return _p


class _FakeArtifacts:
    """Drop-in for ArtifactStore: no filesystem/GCS. Optionally raises."""

    def __init__(self, ref=None, boom=False):
        self._ref = ref if ref is not None else {"backend": "local", "key": "k", "uri": "file:///k"}
        self._boom = boom

    def put_text(self, **kw):
        if self._boom:
            raise RuntimeError("artifact store down")
        return self._ref


def _fake_artifacts(monkeypatch, *, ref=None, boom=False):
    monkeypatch.setattr(pipe, "ArtifactStore", lambda: _FakeArtifacts(ref=ref, boom=boom))


async def _add_trend(session, *, brand=REAL_BRAND, status="proposed", suppressed=False):
    t = Trend(brand=brand, cluster_key=CLUSTER_PREFIX + uuid.uuid4().hex,
              headline="Test headline", status=status, suppressed=suppressed,
              evidence=list(_EVIDENCE))
    session.add(t)
    await session.flush()
    return t


async def _add_pipeline(session, *, brand=REAL_BRAND, trend_id=None,
                        status=PipelineStatus.GENERATING.value, content_types=("article",)):
    p = ContentPipeline(trend_id=trend_id, brand=brand, status=status,
                        requested_by=MARK, content_types=list(content_types))
    session.add(p)
    await session.flush()
    return p


async def _add_job(session, pipeline, *, status=ContentJobStatus.RUNNING.value,
                   content_type="article", transport="llm", external_ref=None,
                   cost=None, preview_meta=None, preview_ref=None):
    j = ContentJob(pipeline_id=pipeline.id, content_type=content_type, transport=transport,
                   status=status, external_ref=external_ref, cost=cost,
                   preview_meta=preview_meta, preview_ref=preview_ref,
                   updated_at=datetime.now(timezone.utc))
    session.add(j)
    await session.flush()
    return j


async def _committed(brand=REAL_BRAND, *, trend_status="approved",
                     pipeline_status=PipelineStatus.GENERATING.value,
                     job_status=ContentJobStatus.RUNNING.value):
    """Seed a trend+pipeline+one job and COMMIT (for sweep / _mark_failed_safely,
    which open their own transactions). Returns (pipeline_id, job_id, trend_id)."""
    async with RunContext.open() as c:
        t = await _add_trend(c.session, brand=brand, status=trend_status)
        p = await _add_pipeline(c.session, brand=brand, trend_id=t.id, status=pipeline_status)
        j = await _add_job(c.session, p, status=job_status)
        return p.id, j.id, t.id


async def _jobs_of(session, pipeline_id):
    """Read a pipeline's jobs with an explicit query. ``approve_and_start`` loads
    ``pipeline.jobs`` (empty) before it inserts jobs by raw FK, so the ORM's
    in-memory collection stays stale within the same transaction — a fresh query
    sees the flushed rows."""
    return list((await session.execute(
        select(ContentJob).where(ContentJob.pipeline_id == pipeline_id)
        .order_by(ContentJob.id))).scalars().all())


async def _job_row(job_id):
    async with RunContext.open() as c:
        return await PipelineRepo(c.session).get_job(job_id)


async def _pipeline_row(pipeline_id):
    async with RunContext.open() as c:
        return await PipelineRepo(c.session).get(pipeline_id)


def _events(mock):
    """The 3rd positional arg (the event name) of each notify_trend_event call."""
    return [c.args[2] for c in mock.await_args_list if len(c.args) >= 3]


# --------------------------------------------------------------------------- #
# approve_and_start
# --------------------------------------------------------------------------- #

async def test_approve_queues_one_job_per_type_and_advances_states(db_ctx, monkeypatch, _no_slack):
    monkeypatch.setattr(personas_mod, "pick_persona", _pick(None))
    t = await _add_trend(db_ctx.session, status="proposed")
    p = await PipelineRepo(db_ctx.session).create(
        trend_id=t.id, brand=REAL_BRAND, content_types=["article", "social_post"], requested_by=MARK)

    await pipe.approve_and_start(db_ctx, p.id, USER)

    fresh = await PipelineRepo(db_ctx.session).get(p.id)
    assert fresh.status == PipelineStatus.GENERATING.value
    assert fresh.approved_by == USER and fresh.approved_at is not None
    jobs = await _jobs_of(db_ctx.session, p.id)
    assert sorted(j.content_type for j in jobs) == ["article", "social_post"]
    assert all(j.status == ContentJobStatus.QUEUED.value for j in jobs)
    assert all(j.transport == "llm" for j in jobs)            # default transport_for
    assert all(j.persona_id is None for j in jobs)            # pick_persona -> None
    ft = await TrendRepo(db_ctx.session).get(t.id)
    assert ft.status == "approved"
    assert "pipeline_approved" in _events(_no_slack)


async def test_approve_resolves_persona_for_every_job(db_ctx, monkeypatch):
    monkeypatch.setattr(personas_mod, "pick_persona", _pick(SimpleNamespace(id=4242)))
    t = await _add_trend(db_ctx.session, status="proposed")
    p = await PipelineRepo(db_ctx.session).create(
        trend_id=t.id, brand=REAL_BRAND, content_types=["article", "social_post"], requested_by=MARK)

    await pipe.approve_and_start(db_ctx, p.id, USER)

    jobs = await _jobs_of(db_ctx.session, p.id)
    assert {j.persona_id for j in jobs} == {4242}


async def test_approve_content_types_override_and_instructions(db_ctx, monkeypatch):
    monkeypatch.setattr(personas_mod, "pick_persona", _pick(None))
    t = await _add_trend(db_ctx.session, status="proposed")
    p = await PipelineRepo(db_ctx.session).create(
        trend_id=t.id, brand=REAL_BRAND, content_types=["article"], requested_by=MARK)

    await pipe.approve_and_start(db_ctx, p.id, USER,
                                 content_types=["video_script"], instructions="  keep it punchy ")

    fresh = await PipelineRepo(db_ctx.session).get(p.id)
    jobs = await _jobs_of(db_ctx.session, p.id)
    assert [j.content_type for j in jobs] == ["video_script"]
    assert fresh.instructions == "keep it punchy"


async def test_approve_missing_pipeline_raises(db_ctx, monkeypatch):
    monkeypatch.setattr(personas_mod, "pick_persona", _pick(None))
    with pytest.raises(LifecycleError, match="not found"):
        await pipe.approve_and_start(db_ctx, 999_999_999, USER)


async def test_approve_refused_when_trend_not_open(db_ctx, monkeypatch):
    monkeypatch.setattr(personas_mod, "pick_persona", _pick(None))
    t = await _add_trend(db_ctx.session, status="completed")  # terminal -> not open
    p = await PipelineRepo(db_ctx.session).create(
        trend_id=t.id, brand=REAL_BRAND, content_types=["article"], requested_by=MARK)
    with pytest.raises(LifecycleError, match="decline or close"):
        await pipe.approve_and_start(db_ctx, p.id, USER)


async def test_approve_refused_when_trend_suppressed(db_ctx, monkeypatch):
    monkeypatch.setattr(personas_mod, "pick_persona", _pick(None))
    t = await _add_trend(db_ctx.session, status="proposed", suppressed=True)
    p = await PipelineRepo(db_ctx.session).create(
        trend_id=t.id, brand=REAL_BRAND, content_types=["article"], requested_by=MARK)
    with pytest.raises(LifecycleError, match="suppressed"):
        await pipe.approve_and_start(db_ctx, p.id, USER)


async def test_approve_swallows_invalid_trend_transition(db_ctx, monkeypatch):
    # 'detected' is open (approval proceeds) but detected -> approved is NOT a legal
    # trend transition, so TrendRepo.set_status raises and pipeline.py logs it as
    # non-fatal: the pipeline still generates, the trend keeps its status.
    monkeypatch.setattr(personas_mod, "pick_persona", _pick(None))
    t = await _add_trend(db_ctx.session, status="detected")
    p = await PipelineRepo(db_ctx.session).create(
        trend_id=t.id, brand=REAL_BRAND, content_types=["article"], requested_by=MARK)

    await pipe.approve_and_start(db_ctx, p.id, USER)

    fresh = await PipelineRepo(db_ctx.session).get(p.id)
    assert fresh.status == PipelineStatus.GENERATING.value
    ft = await TrendRepo(db_ctx.session).get(t.id)
    assert ft.status == "detected"  # unchanged — transition was refused + swallowed


async def test_approve_with_no_trend(db_ctx, monkeypatch, _no_slack):
    monkeypatch.setattr(personas_mod, "pick_persona", _pick(None))
    p = await PipelineRepo(db_ctx.session).create(
        trend_id=None, brand=REAL_BRAND, content_types=["article"], requested_by=MARK)

    await pipe.approve_and_start(db_ctx, p.id, USER)

    fresh = await PipelineRepo(db_ctx.session).get(p.id)
    assert fresh.status == PipelineStatus.GENERATING.value
    assert len(await _jobs_of(db_ctx.session, p.id)) == 1
    assert "pipeline_approved" in _events(_no_slack)


# --------------------------------------------------------------------------- #
# decline_pipeline
# --------------------------------------------------------------------------- #

async def test_decline_proposed_trend_follows_to_declined(db_ctx, _no_slack):
    t = await _add_trend(db_ctx.session, status="proposed")
    p = await PipelineRepo(db_ctx.session).create(
        trend_id=t.id, brand=REAL_BRAND, content_types=["article"], requested_by=MARK)

    out = await pipe.decline_pipeline(db_ctx, p.id, USER, reason="off-brand")

    assert out.status == PipelineStatus.DECLINED.value
    assert out.declined_by == USER and out.close_reason == "off-brand"
    ft = await TrendRepo(db_ctx.session).get(t.id)
    assert ft.status == "declined"
    assert "pipeline_declined" in _events(_no_slack)


async def test_decline_non_proposed_trend_keeps_its_status(db_ctx):
    # A trend that is only 'detected' (not yet proposed) does not follow the
    # request into declined — it may still have other work.
    t = await _add_trend(db_ctx.session, status="detected")
    p = await PipelineRepo(db_ctx.session).create(
        trend_id=t.id, brand=REAL_BRAND, content_types=["article"], requested_by=MARK)

    await pipe.decline_pipeline(db_ctx, p.id, USER)

    ft = await TrendRepo(db_ctx.session).get(t.id)
    assert ft.status == "detected"


async def test_decline_pipeline_without_trend(db_ctx):
    p = await PipelineRepo(db_ctx.session).create(
        trend_id=None, brand=REAL_BRAND, content_types=["article"], requested_by=MARK)

    out = await pipe.decline_pipeline(db_ctx, p.id, USER)

    assert out.status == PipelineStatus.DECLINED.value


# --------------------------------------------------------------------------- #
# _run_job — generation / preview / guards
# --------------------------------------------------------------------------- #

async def test_run_job_missing_job_returns_failed(db_ctx):
    assert await pipe._run_job(db_ctx, 999_999_999) == "failed"


async def test_run_job_orphan_job_returns_failed(db_ctx, monkeypatch):
    gen = AsyncMock()
    monkeypatch.setattr(pipe, "generate", gen)
    j = ContentJob(pipeline_id=None, content_type="article", transport="llm",
                   status=ContentJobStatus.RUNNING.value, updated_at=datetime.now(timezone.utc))
    db_ctx.session.add(j)
    await db_ctx.session.flush()

    assert await pipe._run_job(db_ctx, j.id) == "failed"
    gen.assert_not_awaited()  # bailed before generation


async def test_run_job_kill_switch_refuses(db_ctx, monkeypatch):
    monkeypatch.setattr(db_ctx.settings, "kill_switch", True)
    gen = AsyncMock()
    monkeypatch.setattr(pipe, "generate", gen)
    t = await _add_trend(db_ctx.session, status="approved")
    p = await _add_pipeline(db_ctx.session, trend_id=t.id)
    j = await _add_job(db_ctx.session, p)

    assert await pipe._run_job(db_ctx, j.id) == "failed"
    gen.assert_not_awaited()
    fresh = await PipelineRepo(db_ctx.session).get_job(j.id)
    assert fresh.status == ContentJobStatus.FAILED.value
    assert "kill switch" in fresh.error
    assert (await PipelineRepo(db_ctx.session).get(p.id)).status == PipelineStatus.FAILED.value


async def test_run_job_missing_trend_fails(db_ctx, monkeypatch):
    monkeypatch.setattr(pipe, "generate", AsyncMock())
    p = await _add_pipeline(db_ctx.session, trend_id=None)
    j = await _add_job(db_ctx.session, p)

    assert await pipe._run_job(db_ctx, j.id) == "failed"
    fresh = await PipelineRepo(db_ctx.session).get_job(j.id)
    assert fresh.status == ContentJobStatus.FAILED.value
    assert "trend record missing" in fresh.error


async def test_run_job_pending_keeps_running_and_checkpoints_ref(db_ctx, monkeypatch):
    monkeypatch.setattr(pipe, "generate",
                        _gen(GenerationResult(ok=True, pending=True, external_ref={"topic_id": "T9"})))
    t = await _add_trend(db_ctx.session, status="approved")
    p = await _add_pipeline(db_ctx.session, trend_id=t.id)
    j = await _add_job(db_ctx.session, p)

    assert await pipe._run_job(db_ctx, j.id) == "pending"
    fresh = await PipelineRepo(db_ctx.session).get_job(j.id)
    assert fresh.status == ContentJobStatus.RUNNING.value      # still running, awaits re-poll
    assert fresh.external_ref == {"topic_id": "T9"}


async def test_run_job_generation_failure_marks_failed(db_ctx, monkeypatch):
    monkeypatch.setattr(pipe, "generate",
                        _gen(GenerationResult(ok=False, error="LLM refused", external_ref={"x": 1})))
    t = await _add_trend(db_ctx.session, status="approved")
    p = await _add_pipeline(db_ctx.session, trend_id=t.id)
    j = await _add_job(db_ctx.session, p)

    assert await pipe._run_job(db_ctx, j.id) == "failed"
    fresh = await PipelineRepo(db_ctx.session).get_job(j.id)
    assert fresh.status == ContentJobStatus.FAILED.value
    assert fresh.error == "LLM refused"
    assert fresh.external_ref == {"x": 1}


async def test_run_job_happy_preview_cost_memory_notify(db_ctx, monkeypatch, _no_slack):
    _fake_artifacts(monkeypatch, ref={"backend": "local", "key": "kk", "uri": "file:///kk"})
    monkeypatch.setattr(pipe, "generate", _gen(GenerationResult(
        ok=True, preview_markdown="# Title\n\nbody words here",
        preview_meta={"title": "Title", "word_count": 3}, cost_micros=500_000)))
    t = await _add_trend(db_ctx.session, status="approved")
    p = await _add_pipeline(db_ctx.session, trend_id=t.id)
    j = await _add_job(db_ctx.session, p)

    assert await pipe._run_job(db_ctx, j.id) == "ok"

    repo = PipelineRepo(db_ctx.session)
    fresh = await repo.get_job(j.id)
    assert fresh.status == ContentJobStatus.PREVIEW_READY.value
    assert fresh.preview_ref == {"backend": "local", "key": "kk", "uri": "file:///kk"}
    assert fresh.preview_meta == {"title": "Title", "word_count": 3}
    assert fresh.cost == {"llm_micros": 500_000}
    assert (await repo.get(p.id)).status == PipelineStatus.PREVIEWS_READY.value

    # preview landed in shared memory
    rows = await db_ctx.store.query(
        brand=REAL_BRAND, types=[EntryType.DISTRIBUTION_DRAFT], source_system="trend_pipeline",
        payload_contains={"pipeline_id": p.id, "job_id": j.id, "kind": "trend_content_draft"})
    assert len(rows) == 1

    # preview-time cost rollup ($0.50) + PREVIEWS_READY notification
    cost = (await db_ctx.session.execute(
        select(PipelineCost).where(PipelineCost.pipeline_run_id == f"pipeline:{p.id}"))).scalar_one()
    assert cost.total_usd == pytest.approx(0.5)
    assert cost.used_style_profile is False
    assert "previews_ready" in _events(_no_slack)


async def test_run_job_survives_artifact_store_failure(db_ctx, monkeypatch):
    _fake_artifacts(monkeypatch, boom=True)   # put_text raises
    monkeypatch.setattr(pipe, "generate", _gen(GenerationResult(
        ok=True, preview_markdown="# T\n\nbody", preview_meta={"title": "T", "word_count": 2},
        cost_micros=1_000)))
    t = await _add_trend(db_ctx.session, status="approved")
    p = await _add_pipeline(db_ctx.session, trend_id=t.id)
    j = await _add_job(db_ctx.session, p)

    assert await pipe._run_job(db_ctx, j.id) == "ok"   # artifact failure is non-fatal
    fresh = await PipelineRepo(db_ctx.session).get_job(j.id)
    assert fresh.status == ContentJobStatus.PREVIEW_READY.value
    assert fresh.preview_ref is None


async def test_run_job_zero_cost_leaves_cost_null(db_ctx, monkeypatch):
    _fake_artifacts(monkeypatch)
    monkeypatch.setattr(pipe, "generate", _gen(GenerationResult(
        ok=True, preview_markdown="# T\n\nb", preview_meta={"title": "T", "word_count": 1},
        cost_micros=0)))
    t = await _add_trend(db_ctx.session, status="approved")
    p = await _add_pipeline(db_ctx.session, trend_id=t.id)
    j = await _add_job(db_ctx.session, p)

    assert await pipe._run_job(db_ctx, j.id) == "ok"
    fresh = await PipelineRepo(db_ctx.session).get_job(j.id)
    assert fresh.cost is None


async def test_run_job_cost_rollup_error_is_swallowed(db_ctx, monkeypatch):
    _fake_artifacts(monkeypatch)
    monkeypatch.setattr(pipe, "generate", _gen(GenerationResult(
        ok=True, preview_markdown="# T\n\nb", preview_meta={"title": "T", "word_count": 1},
        cost_micros=10_000)))
    monkeypatch.setattr(pipe, "_record_pipeline_cost", AsyncMock(side_effect=RuntimeError("rollup boom")))
    t = await _add_trend(db_ctx.session, status="approved")
    p = await _add_pipeline(db_ctx.session, trend_id=t.id)
    j = await _add_job(db_ctx.session, p)

    assert await pipe._run_job(db_ctx, j.id) == "ok"   # best-effort rollup never fails the preview
    fresh = await PipelineRepo(db_ctx.session).get_job(j.id)
    assert fresh.status == ContentJobStatus.PREVIEW_READY.value


# --------------------------------------------------------------------------- #
# run_job_sweep / _mark_failed_safely  (own-transaction orchestration)
# --------------------------------------------------------------------------- #

def _patch_claim(monkeypatch, queued_ids):
    """Isolate the sweep from other rows in the shared DB: feed it exactly our
    job id and make the stuck/dead reapers no-ops. The raw SKIP-LOCKED SQL lives
    in repo.py; here we exercise pipeline.py's per-job orchestration."""
    async def claim_queued(self, limit=5):
        return list(queued_ids)

    async def claim_stuck(self, cutoff, limit=5):
        return []

    async def reap(self, cutoff):
        return 0

    monkeypatch.setattr(PipelineRepo, "claim_queued", claim_queued)
    monkeypatch.setattr(PipelineRepo, "claim_stuck_running", claim_stuck)
    monkeypatch.setattr(PipelineRepo, "reap_dead_running", reap)


async def test_sweep_nothing_to_do(require_db, monkeypatch):
    _patch_claim(monkeypatch, [])
    assert await pipe.run_job_sweep() == {"ok": 0, "pending": 0, "failed": 0}


async def test_sweep_processes_claimed_job(require_db, monkeypatch):
    _fake_artifacts(monkeypatch)
    monkeypatch.setattr(pipe, "generate", _gen(GenerationResult(
        ok=True, preview_markdown="# S\n\nbody", preview_meta={"title": "S", "word_count": 2},
        cost_micros=1_000)))
    pid, jid, _ = await _committed()
    _patch_claim(monkeypatch, [jid])

    assert await pipe.run_job_sweep() == {"ok": 1, "pending": 0, "failed": 0}
    assert (await _job_row(jid)).status == ContentJobStatus.PREVIEW_READY.value


async def test_sweep_isolates_a_crashing_job(require_db, monkeypatch):
    # A worker-level crash (not a handled generation failure) is caught by the
    # sweep and recorded via _mark_failed_safely, in a fresh transaction.
    monkeypatch.setattr(pipe, "generate", _gen_raises(RuntimeError("worker exploded")))
    pid, jid, _ = await _committed()
    _patch_claim(monkeypatch, [jid])

    assert await pipe.run_job_sweep() == {"ok": 0, "pending": 0, "failed": 1}
    row = await _job_row(jid)
    assert row.status == ContentJobStatus.FAILED.value
    assert row.error.startswith("worker error:")


async def test_mark_failed_safely_rolls_up_pipeline(require_db):
    pid, jid, _ = await _committed()
    await pipe._mark_failed_safely(jid, "boom detail")
    row = await _job_row(jid)
    assert row.status == ContentJobStatus.FAILED.value
    assert row.error == "boom detail"
    assert (await _pipeline_row(pid)).status == PipelineStatus.FAILED.value


async def test_mark_failed_safely_truncates_and_handles_orphan(require_db):
    async with RunContext.open() as c:
        j = ContentJob(pipeline_id=None, content_type="article", transport="llm",
                       status=ContentJobStatus.RUNNING.value, updated_at=datetime.now(timezone.utc))
        c.session.add(j)
        await c.session.flush()
        jid = j.id
    await pipe._mark_failed_safely(jid, "x" * 800)
    row = await _job_row(jid)
    assert row.status == ContentJobStatus.FAILED.value
    assert len(row.error) == 500   # error[:500]
    # cleanup (no MARK pipeline owns this orphan job)
    async with RunContext.open() as c:
        await c.session.execute(delete(ContentJob).where(ContentJob.id == jid))
        await c.session.commit()


# --------------------------------------------------------------------------- #
# publish_job — the second human gate
# --------------------------------------------------------------------------- #

async def test_publish_rejects_non_human_actor(db_ctx):
    with pytest.raises(LifecycleError, match="human actor"):
        await pipe.publish_job(db_ctx, 1, "orchestrator")


async def test_publish_refused_under_kill_switch(db_ctx, monkeypatch):
    monkeypatch.setattr(db_ctx.settings, "kill_switch", True)
    with pytest.raises(LifecycleError, match="kill switch"):
        await pipe.publish_job(db_ctx, 1, USER)


async def test_publish_missing_job_raises(db_ctx):
    with pytest.raises(LifecycleError, match="not found"):
        await pipe.publish_job(db_ctx, 999_999_999, USER)


async def test_publish_orphan_job_raises(db_ctx):
    j = ContentJob(pipeline_id=None, content_type="article", transport="llm",
                   status=ContentJobStatus.APPROVED.value, updated_at=datetime.now(timezone.utc))
    db_ctx.session.add(j)
    await db_ctx.session.flush()
    with pytest.raises(LifecycleError, match="not found"):
        await pipe.publish_job(db_ctx, j.id, USER)


async def test_publish_blocked_on_terminal_pipeline(db_ctx):
    p = await _add_pipeline(db_ctx.session, status=PipelineStatus.CLOSED.value)
    j = await _add_job(db_ctx.session, p, status=ContentJobStatus.APPROVED.value)
    with pytest.raises(LifecycleError, match="reopen is not allowed"):
        await pipe.publish_job(db_ctx, j.id, USER)


async def test_publish_requires_approved_preview(db_ctx):
    t = await _add_trend(db_ctx.session, status="approved")
    p = await _add_pipeline(db_ctx.session, trend_id=t.id, status=PipelineStatus.PREVIEWS_READY.value)
    j = await _add_job(db_ctx.session, p, status=ContentJobStatus.PREVIEW_READY.value)
    with pytest.raises(LifecycleError, match="approve the preview"):
        await pipe.publish_job(db_ctx, j.id, USER)


async def test_publish_manual_handoff_completes_pipeline_and_trend(db_ctx, _no_slack):
    t = await _add_trend(db_ctx.session, status="approved")
    p = await _add_pipeline(db_ctx.session, trend_id=t.id, status=PipelineStatus.PREVIEWS_READY.value)
    j = await _add_job(db_ctx.session, p, status=ContentJobStatus.APPROVED.value,
                       cost={"llm_micros": 300_000}, preview_ref={"uri": "file:///d"})

    out = await pipe.publish_job(db_ctx, j.id, USER)

    assert out.status == ContentJobStatus.PUBLISHED.value
    assert out.result_ref["mode"] == "manual_handoff"
    assert out.reviewed_by == USER and out.reviewed_at is not None
    repo = PipelineRepo(db_ctx.session)
    assert (await repo.get(p.id)).status == PipelineStatus.PUBLISHED.value
    assert (await TrendRepo(db_ctx.session).get(t.id)).status == "completed"
    # publish-time cost rollup
    cost = (await db_ctx.session.execute(
        select(PipelineCost).where(PipelineCost.pipeline_run_id == f"pipeline:{p.id}"))).scalar_one()
    assert cost.total_usd == pytest.approx(0.3)
    # DECISION entry recorded
    rows = await db_ctx.store.query(
        brand=REAL_BRAND, types=[EntryType.DECISION], source_system="trend_pipeline",
        payload_contains={"pipeline_id": p.id, "kind": "trend_content_published"})
    assert len(rows) == 1
    assert "content_published" in _events(_no_slack)


async def test_publish_hc_viral_uses_emaki_push(db_ctx, monkeypatch):
    emaki = AsyncMock(return_value={"mode": "emaki_unpublished_draft", "topic_id": "T1"})
    monkeypatch.setattr(pipe, "_emaki_push", emaki)
    p = await _add_pipeline(db_ctx.session, trend_id=None, status=PipelineStatus.PREVIEWS_READY.value)
    j = await _add_job(db_ctx.session, p, status=ContentJobStatus.APPROVED.value,
                       transport="hc_viral_hits", external_ref={"topic_id": "T1"})

    out = await pipe.publish_job(db_ctx, j.id, USER)

    emaki.assert_awaited_once()
    assert out.result_ref == {"mode": "emaki_unpublished_draft", "topic_id": "T1"}
    assert out.status == ContentJobStatus.PUBLISHED.value


async def test_publish_partial_when_a_sibling_was_rejected(db_ctx):
    t = await _add_trend(db_ctx.session, status="approved")
    p = await _add_pipeline(db_ctx.session, trend_id=t.id, status=PipelineStatus.PREVIEWS_READY.value,
                            content_types=["article", "social_post"])
    await _add_job(db_ctx.session, p, status=ContentJobStatus.REJECTED.value, content_type="social_post")
    approved = await _add_job(db_ctx.session, p, status=ContentJobStatus.APPROVED.value,
                              content_type="article")

    await pipe.publish_job(db_ctx, approved.id, USER)

    assert (await PipelineRepo(db_ctx.session).get(p.id)).status == PipelineStatus.PARTIALLY_PUBLISHED.value
    assert (await TrendRepo(db_ctx.session).get(t.id)).status == "completed"


async def test_publish_without_trend_still_rolls_cost(db_ctx):
    p = await _add_pipeline(db_ctx.session, trend_id=None, status=PipelineStatus.PREVIEWS_READY.value)
    j = await _add_job(db_ctx.session, p, status=ContentJobStatus.APPROVED.value,
                       cost={"llm_micros": 120_000})

    await pipe.publish_job(db_ctx, j.id, USER)

    assert (await PipelineRepo(db_ctx.session).get(p.id)).status == PipelineStatus.PUBLISHED.value
    cost = (await db_ctx.session.execute(
        select(PipelineCost).where(PipelineCost.pipeline_run_id == f"pipeline:{p.id}"))).scalar_one()
    assert cost.total_usd == pytest.approx(0.12)


# --------------------------------------------------------------------------- #
# _record_pipeline_cost / _human_equiv_usd  (synthetic brand, no store.write)
# --------------------------------------------------------------------------- #

async def _cost_pipeline(session, jobs):
    p = ContentPipeline(brand=SYNTH_BRAND, status=PipelineStatus.PUBLISHED.value,
                        requested_by=MARK, content_types=["article"])
    session.add(p)
    await session.flush()
    for spec in jobs:
        session.add(ContentJob(
            pipeline_id=p.id, content_type=spec.get("content_type", "article"),
            status=ContentJobStatus.PUBLISHED.value, cost=spec.get("cost"),
            preview_meta=spec.get("preview_meta"), updated_at=datetime.now(timezone.utc)))
    await session.flush()
    return await PipelineRepo(session).get(p.id)


async def test_record_cost_llm_only_and_idempotent(db_ctx):
    pipeline = await _cost_pipeline(db_ctx.session, [
        {"cost": {"llm_micros": 250_000}, "preview_meta": {"word_count": 400}},
        {"content_type": "social_post", "cost": {"llm_micros": 750_000},
         "preview_meta": {"word_count": 600}},
    ])
    run_id = f"pipeline:{pipeline.id}"

    await pipe._record_pipeline_cost(db_ctx, pipeline)
    rows = (await db_ctx.session.execute(
        select(PipelineCost).where(PipelineCost.pipeline_run_id == run_id))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.total_usd == pytest.approx(1.0)
    assert row.cost_breakdown == {"llm_usd": 1.0, "ahrefs_usd": 0.0, "bq_usd": 0.0, "other_usd": 0.0}
    assert row.human_equiv_usd is None and row.savings_usd is None
    assert row.used_style_profile is False

    # second rollup upserts the same row, never duplicates
    await pipe._record_pipeline_cost(db_ctx, pipeline)
    rows = (await db_ctx.session.execute(
        select(PipelineCost).where(PipelineCost.pipeline_run_id == run_id))).scalars().all()
    assert len(rows) == 1


async def test_record_cost_flags_style_profile(db_ctx):
    prof = WriterStyleProfile(brand=SYNTH_BRAND, version=1, source_authors=["a"], features={},
                              active=False)
    db_ctx.session.add(prof)
    await db_ctx.session.flush()
    pipeline = await _cost_pipeline(db_ctx.session, [
        {"cost": {"llm_micros": 100_000},
         "preview_meta": {"word_count": 100, "used_style_profile": True, "style_profile_id": prof.id}},
    ])

    await pipe._record_pipeline_cost(db_ctx, pipeline)
    row = (await db_ctx.session.execute(
        select(PipelineCost).where(PipelineCost.pipeline_run_id == f"pipeline:{pipeline.id}"))).scalar_one()
    assert row.used_style_profile is True
    assert row.style_profile_id == prof.id


async def test_record_cost_with_human_baseline_computes_savings(db_ctx):
    db_ctx.session.add(WriterPayBaseline(brand=SYNTH_BRAND, author=None, usd_per_article=45.0))
    await db_ctx.session.flush()
    pipeline = await _cost_pipeline(db_ctx.session, [
        {"cost": {"llm_micros": 1_000_000}, "preview_meta": {"word_count": 500}},
    ])

    await pipe._record_pipeline_cost(db_ctx, pipeline)
    row = (await db_ctx.session.execute(
        select(PipelineCost).where(PipelineCost.pipeline_run_id == f"pipeline:{pipeline.id}"))).scalar_one()
    assert row.total_usd == pytest.approx(1.0)
    assert row.human_equiv_usd == pytest.approx(45.0)
    assert row.savings_usd == pytest.approx(44.0)


async def test_human_equiv_none_article_and_perword(db_ctx):
    # no baseline -> None
    assert await pipe._human_equiv_usd(db_ctx.session, SYNTH_BRAND, 1000) is None

    # flat per-article wins
    db_ctx.session.add(WriterPayBaseline(brand=SYNTH_BRAND, author=None, usd_per_article=30.0))
    await db_ctx.session.flush()
    assert await pipe._human_equiv_usd(db_ctx.session, SYNTH_BRAND, 1000) == pytest.approx(30.0)


async def test_human_equiv_perword_fallback_needs_words(db_ctx):
    db_ctx.session.add(WriterPayBaseline(brand=SYNTH_BRAND, author=None, usd_per_word=0.05))
    await db_ctx.session.flush()
    assert await pipe._human_equiv_usd(db_ctx.session, SYNTH_BRAND, 1000) == pytest.approx(50.0)
    # per-word rate but zero words cannot be priced
    assert await pipe._human_equiv_usd(db_ctx.session, SYNTH_BRAND, 0) is None
