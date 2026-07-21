"""DB-backed tests for :mod:`switchboard.trends.repo` (TrendRepo + PipelineRepo).

These run against a real Postgres (the schema is assumed migrated). They self-skip
when no DB is reachable, mirroring ``tests/integration/test_pipeline.py``.

Isolation: every row created here is tagged with a brand that starts with
``utest_repo`` and swept by the autouse ``_scrub_repo_rows`` fixture in a fresh
RunContext. Methods that mutate rows *globally* and cannot be cleaned by marker
(``TrendRepo.expire_stale`` and the raw-SQL ``PipelineRepo.claim_queued`` /
``claim_stuck_running`` / ``reap_dead_running``) are exercised inside a session
that is always rolled back (:func:`_rb_session`), so they never persist.

pytest is ``asyncio_mode="auto"`` — async tests need no decorator. The shared
``db_ctx`` fixture (tests/conftest.py) provides a committing RunContext and the
skip-if-no-DB behaviour.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from switchboard.context import RunContext
from switchboard.db.base import get_sessionmaker
from switchboard.db.models import ContentJob, ContentPipeline, Trend, TrendActivity
from switchboard.trends.lifecycle import LifecycleError
from switchboard.trends.repo import PipelineRepo, TrendRepo

MARKER = "utest_repo"
USER = "andrew.marks@valnetinc.com"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ck(tag: str = "ck") -> str:
    """A unique, marker-tagged cluster key."""
    return f"{MARKER}_{tag}_{uuid4().hex[:10]}"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
async def _scrub_repo_rows():
    """Delete every row this module writes (matched by the ``utest_repo`` brand
    prefix), in a fresh RunContext after each test. No-op when the DB is down."""
    yield
    try:
        async with RunContext.open() as c:
            s = c.session
            await s.execute(delete(ContentJob).where(ContentJob.pipeline_id.in_(
                select(ContentPipeline.id).where(ContentPipeline.brand.like(f"{MARKER}%")))))
            await s.execute(delete(ContentPipeline).where(ContentPipeline.brand.like(f"{MARKER}%")))
            await s.execute(delete(TrendActivity).where(TrendActivity.trend_id.in_(
                select(Trend.id).where(Trend.brand.like(f"{MARKER}%")))))
            await s.execute(delete(Trend).where(Trend.brand.like(f"{MARKER}%")))
            await s.commit()
    except Exception:  # noqa: BLE001 — no DB / nothing to scrub
        pass


@asynccontextmanager
async def _rb_session():
    """A session whose work is ALWAYS rolled back — for repo methods that mutate
    rows globally (they cannot be isolated by marker). Skips if no DB."""
    maker = get_sessionmaker()
    session = maker()
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await session.close()
        pytest.skip(f"no reachable Postgres: {exc}")
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()


async def _add_trend(s, *, brand, cluster_key=None, headline=None, status="detected",
                     score=0.0, **kw) -> Trend:
    t = Trend(brand=brand, cluster_key=cluster_key or _ck(),
              headline=headline if headline is not None else f"{MARKER} headline",
              status=status, score=score, **kw)
    s.add(t)
    await s.flush()
    return t


async def _add_pipeline(s, *, brand, status="pending_approval", trend_id=None,
                        content_types=None, **kw) -> ContentPipeline:
    p = ContentPipeline(brand=brand, status=status, trend_id=trend_id,
                        content_types=content_types or ["article"], **kw)
    s.add(p)
    await s.flush()
    return p


async def _add_job(s, *, pipeline_id, content_type="article", transport="llm",
                   status="queued", **kw) -> ContentJob:
    j = ContentJob(pipeline_id=pipeline_id, content_type=content_type,
                   transport=transport, status=status, **kw)
    s.add(j)
    await s.flush()
    return j


# =========================================================================== #
# TrendRepo — reads
# =========================================================================== #
async def test_trend_get_hit_eager_loads_pipelines_and_jobs(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_get"
    t = await _add_trend(s, brand=b)
    p = await _add_pipeline(s, brand=b, trend_id=t.id)
    j = await _add_job(s, pipeline_id=p.id)

    got = await r.get(t.id)
    assert got is not None and got.id == t.id
    # eager-loaded chain (accessing these must not raise MissingGreenlet)
    assert [pl.id for pl in got.pipelines] == [p.id]
    assert [jb.id for jb in got.pipelines[0].jobs] == [j.id]


async def test_trend_get_missing_returns_none(db_ctx):
    assert await TrendRepo(db_ctx.session).get(2_000_000_111) is None


async def test_trend_list_brand_status_limit_and_order(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_list"
    other = f"{MARKER}_list_other"
    low = await _add_trend(s, brand=b, score=1.0, status="detected")
    high = await _add_trend(s, brand=b, score=99.0, status="detected")
    mid = await _add_trend(s, brand=b, score=50.0, status="proposed")
    await _add_trend(s, brand=other, score=100.0)  # different brand — excluded

    # brand filter + order by score desc
    rows = await r.list(brand=b)
    ids = [t.id for t in rows]
    assert set(ids) == {low.id, high.id, mid.id}
    assert ids == [high.id, mid.id, low.id]  # 99 > 50 > 1

    # status filter
    only_detected = await r.list(brand=b, statuses=["detected"])
    assert {t.id for t in only_detected} == {low.id, high.id}

    # limit takes the top-N by the ordering
    assert [t.id for t in await r.list(brand=b, limit=2)] == [high.id, mid.id]

    # no-filter call exercises the brand=None / statuses=None branches
    assert isinstance(await r.list(limit=1), list)
    assert isinstance(await r.list(statuses=["detected"], limit=1), list)


async def test_trend_list_empty_when_no_match(db_ctx):
    r = TrendRepo(db_ctx.session)
    assert await r.list(brand=f"{MARKER}_nolist_{uuid4().hex}") == []


async def test_find_by_cluster_key_returns_newest_and_is_brand_scoped(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_fck"
    ck = _ck("shared")
    now = _now()
    await _add_trend(s, brand=b, cluster_key=ck, created_at=now - timedelta(hours=3))
    newest = await _add_trend(s, brand=b, cluster_key=ck, created_at=now - timedelta(minutes=1))

    found = await r.find_by_cluster_key(ck, brand=b)
    assert found is not None and found.id == newest.id  # newest by created_at

    # brand-scoped: same cluster key, wrong brand → None
    assert await r.find_by_cluster_key(ck, brand=f"{MARKER}_fck_wrong") is None
    # unknown cluster key → None
    assert await r.find_by_cluster_key(_ck("missing"), brand=b) is None


# =========================================================================== #
# TrendRepo.upsert
# =========================================================================== #
async def test_upsert_creates_new_trend(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_ins"
    ck = _ck()
    trend, created = await r.upsert(
        brand=b, cluster_key=ck, headline="Big news", score=42.0,
        score_breakdown={"a": 1}, velocity=2.5, source_count=4, signal_count=7,
        covered_by_us=None, entities={"oems": ["x"]}, evidence=[{"i": 0}],
        ttl_hours=48, dedup_days=7, origin="scout",
    )
    assert created is True
    assert trend is not None
    assert trend.status == "detected"
    assert trend.origin == "scout"
    assert trend.score == 42.0
    assert trend.last_seen_at is not None
    assert trend.expires_at is not None and trend.expires_at > _now()


async def test_upsert_refreshes_open_trend_with_max_semantics(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_refresh"
    ck = _ck()
    first, created = await r.upsert(
        brand=b, cluster_key=ck, headline="Original", score=10.0,
        score_breakdown={"v": 1}, velocity=1.0, source_count=5, signal_count=3,
        covered_by_us=True, entities={}, evidence=[{"i": 0}], ttl_hours=24, dedup_days=7,
    )
    assert created is True

    # Lower score/source_count, higher signal_count, empty headline, covered=None,
    # oversized evidence → max() semantics + 60-item cap + sticky covered/headline.
    big_evidence = [{"i": i} for i in range(70)]
    same, created2 = await r.upsert(
        brand=b, cluster_key=ck, headline="", score=4.0,
        score_breakdown={"v": 2}, velocity=9.0, source_count=2, signal_count=9,
        covered_by_us=None, entities={"e": 1}, evidence=big_evidence,
        ttl_hours=24, dedup_days=7,
    )
    assert created2 is False
    assert same.id == first.id
    assert same.score == 10.0          # max(10, 4)
    assert same.source_count == 5      # max(5, 2)
    assert same.signal_count == 9      # max(3, 9)
    assert same.headline == "Original"  # empty headline kept old
    assert same.covered_by_us is True   # None left it untouched
    assert same.velocity == 9.0         # velocity always overwritten
    assert len(same.evidence) == 60     # capped

    # Third refresh: real headline + covered=False now take effect.
    again, created3 = await r.upsert(
        brand=b, cluster_key=ck, headline="Updated headline", score=20.0,
        score_breakdown={}, velocity=3.0, source_count=1, signal_count=1,
        covered_by_us=False, entities={}, evidence=[], ttl_hours=24, dedup_days=7,
    )
    assert created3 is False
    assert again.score == 20.0
    assert again.headline == "Updated headline"
    assert again.covered_by_us is False
    assert again.evidence == []


async def test_upsert_dedup_suppresses_recent_dismissed_twin(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_dedup"
    ck = _ck()
    # a dismissed twin seen just now → within a 7-day window → suppressed.
    await _add_trend(s, brand=b, cluster_key=ck, status="dismissed", last_seen_at=_now())

    trend, created = await r.upsert(
        brand=b, cluster_key=ck, headline="again", score=5.0, score_breakdown={},
        velocity=1.0, source_count=1, signal_count=1, covered_by_us=None,
        entities={}, evidence=[], ttl_hours=24, dedup_days=7,
    )
    assert (trend, created) == (None, False)


async def test_upsert_dedup_falls_back_to_created_at_when_no_last_seen(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_dedup_ca"
    ck = _ck()
    # dismissed, no last_seen_at → dedup uses created_at (recent) → suppressed.
    await _add_trend(s, brand=b, cluster_key=ck, status="declined",
                     last_seen_at=None, created_at=_now())
    trend, created = await r.upsert(
        brand=b, cluster_key=ck, headline="x", score=1.0, score_breakdown={},
        velocity=0.0, source_count=0, signal_count=0, covered_by_us=None,
        entities={}, evidence=[], ttl_hours=24, dedup_days=30,
    )
    assert (trend, created) == (None, False)


async def test_upsert_after_dedup_window_creates_fresh_trend(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_dedup_expired"
    ck = _ck()
    dismissed = await _add_trend(s, brand=b, cluster_key=ck, status="expired",
                                 last_seen_at=_now())
    # dedup_days=0 → window empty → NOT suppressed → a brand-new trend is created.
    trend, created = await r.upsert(
        brand=b, cluster_key=ck, headline="revived", score=8.0, score_breakdown={},
        velocity=1.0, source_count=1, signal_count=1, covered_by_us=None,
        entities={}, evidence=[], ttl_hours=24, dedup_days=0,
    )
    assert created is True
    assert trend is not None and trend.id != dismissed.id
    assert trend.status == "detected"


# =========================================================================== #
# TrendRepo — status / dossier / dismiss
# =========================================================================== #
async def test_set_status_valid_missing_and_invalid(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_setstatus"
    t = await _add_trend(s, brand=b, status="detected")

    await r.set_status(t.id, "proposed")   # detected → proposed valid
    assert t.status == "proposed"

    await r.set_status(t.id, "proposed")   # same → early-return no-op
    assert t.status == "proposed"

    # missing id → silent no-op (no exception)
    await r.set_status(2_000_000_222, "proposed")

    # invalid transition raises
    with pytest.raises(LifecycleError):
        await r.set_status(t.id, "completed")   # proposed → completed not allowed


async def test_set_dossier_sets_values_and_missing_is_noop(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_dossier"
    t = await _add_trend(s, brand=b)
    dossier = {"summary": "s", "sources": [1, 2]}
    ref = {"artifact": "a1"}
    await r.set_dossier(t.id, dossier, ref)

    # set_dossier is a Core UPDATE (bypasses the ORM identity map) — read columns
    # back directly to see the persisted values.
    row = (await s.execute(
        select(Trend.dossier, Trend.dossier_ref).where(Trend.id == t.id))).one()
    assert row.dossier == dossier
    assert row.dossier_ref == ref

    # missing id → UPDATE hits 0 rows, no error
    await r.set_dossier(2_000_000_333, {"x": 1}, None)


async def test_dismiss_happy_missing_nonhuman_and_terminal(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_dismiss"

    t = await _add_trend(s, brand=b, status="detected")
    out = await r.dismiss(t.id, USER)
    assert out.status == "dismissed"
    assert out.last_seen_at is not None

    # non-human actor rejected
    t2 = await _add_trend(s, brand=b, status="detected")
    with pytest.raises(LifecycleError):
        await r.dismiss(t2.id, "trend_scout")

    # missing trend
    with pytest.raises(LifecycleError):
        await r.dismiss(2_000_000_444, USER)

    # invalid transition from a terminal state
    t3 = await _add_trend(s, brand=b, status="completed")
    with pytest.raises(LifecycleError):
        await r.dismiss(t3.id, USER)


# =========================================================================== #
# TrendRepo.expire_stale  (global mutation → rollback session)
# =========================================================================== #
async def test_expire_stale_expires_open_trend_and_pending_pipeline():
    async with _rb_session() as s:
        r = TrendRepo(s)
        b = f"{MARKER}_expire"
        t = await _add_trend(s, brand=b, status="detected",
                             expires_at=_now() - timedelta(hours=1))
        p = await _add_pipeline(s, brand=b, trend_id=t.id, status="pending_approval")

        n = await r.expire_stale()
        assert n >= 1  # at least my trend (may sweep other stale ones too)

        # trend flipped to expired (Core UPDATE → read column back)
        st = (await s.execute(select(Trend.status).where(Trend.id == t.id))).scalar_one()
        assert st == "expired"

        # its pending trigger request was expired + annotated
        pst = (await s.execute(
            select(ContentPipeline.status).where(ContentPipeline.id == p.id))).scalar_one()
        assert pst == "expired"
        assert p.closed_at is not None
        assert p.close_reason == "trend expired before a decision"
        assert any(e.get("event") == "expired" for e in (p.events or []))


async def test_expire_stale_ignores_future_and_non_open_trends():
    async with _rb_session() as s:
        r = TrendRepo(s)
        b = f"{MARKER}_expire_skip"
        # future expiry → not swept
        future = await _add_trend(s, brand=b, status="detected",
                                  expires_at=_now() + timedelta(days=1))
        # already-approved (non-open for the sweep) even though past expiry → not swept
        approved = await _add_trend(s, brand=b, status="approved",
                                    expires_at=_now() - timedelta(days=1))
        await r.expire_stale()
        assert (await s.execute(
            select(Trend.status).where(Trend.id == future.id))).scalar_one() == "detected"
        assert (await s.execute(
            select(Trend.status).where(Trend.id == approved.id))).scalar_one() == "approved"


# =========================================================================== #
# TrendRepo.counts_by_status
# =========================================================================== #
async def test_counts_by_status_shape_and_reflects_new_rows(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_counts"
    before = await r.counts_by_status()

    await _add_trend(s, brand=b, status="detected")
    await _add_trend(s, brand=b, status="detected")
    await _add_trend(s, brand=b, status="proposed")

    after = await r.counts_by_status()
    assert isinstance(after, dict)
    assert all(isinstance(k, str) and isinstance(v, int) for k, v in after.items())
    assert after.get("detected", 0) >= before.get("detected", 0) + 2
    assert after.get("proposed", 0) >= before.get("proposed", 0) + 1


# =========================================================================== #
# TrendRepo — activity lifecycle (§16.2)
# =========================================================================== #
async def test_record_activity_insert_then_idempotent_update(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_activity"
    t = await _add_trend(s, brand=b)
    today = _now().date()

    await r.record_activity(t.id, today, external_score=50.0, onsite_sessions=100,
                            article_count=3)
    rows = (await s.execute(
        select(TrendActivity).where(TrendActivity.trend_id == t.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].external_score == 50.0
    assert rows[0].onsite_sessions == 100
    assert rows[0].article_count == 3

    # same day → update in place (still one row)
    await r.record_activity(t.id, today, external_score=75.0, onsite_sessions=200,
                            article_count=5)
    rows = (await s.execute(
        select(TrendActivity).where(TrendActivity.trend_id == t.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].external_score == 75.0
    assert rows[0].onsite_sessions == 200
    assert rows[0].article_count == 5

    # defaults overwrite existing values with NULL (documents actual behaviour)
    await r.record_activity(t.id, today)
    await s.refresh(rows[0])
    assert rows[0].external_score is None
    assert rows[0].onsite_sessions is None
    assert rows[0].article_count is None


async def test_activity_series_windowing_order_and_shape(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_series"
    t = await _add_trend(s, brand=b)
    today = _now().date()

    await r.record_activity(t.id, today - timedelta(days=100), external_score=99.0,
                            onsite_sessions=9)   # outside 21-day window
    await r.record_activity(t.id, today - timedelta(days=21), external_score=21.0,
                            onsite_sessions=2)    # boundary — included (>= since)
    await r.record_activity(t.id, today - timedelta(days=1), external_score=10.0,
                            onsite_sessions=1)
    await r.record_activity(t.id, today, external_score=20.0, onsite_sessions=3)

    series = await r.activity_series(t.id, days=21)
    assert [row["as_of"] for row in series] == [
        (today - timedelta(days=21)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
        today.isoformat(),
    ]
    # dict shape: as_of / external_score / onsite_sessions only (no article_count)
    assert set(series[0].keys()) == {"as_of", "external_score", "onsite_sessions"}
    assert series[-1]["external_score"] == 20.0
    assert series[-1]["onsite_sessions"] == 3

    # empty series for an unknown trend
    assert await r.activity_series(2_000_000_555) == []


async def test_set_lifecycle_state_peak_once_and_suppression_rules(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_lifecycle"

    t = await _add_trend(s, brand=b, state="emerging", suppressed=False,
                         suppressed_by=None)
    peak = _now()
    await r.set_lifecycle(t.id, "peak", True, peak_at=peak)
    assert t.state == "peak"
    assert t.suppressed is True          # auto-managed (suppressed_by is NULL)
    assert t.peak_at is not None
    first_peak = t.peak_at

    # peak_at recorded once — a later peak_at does not overwrite it
    await r.set_lifecycle(t.id, "declining", True, peak_at=peak + timedelta(hours=5))
    assert t.state == "declining"
    assert t.peak_at == first_peak

    # human override sticky: suppressed_by set → suppressed is not auto-managed
    t2 = await _add_trend(s, brand=b, state="rising", suppressed=True,
                          suppressed_by=USER)
    await r.set_lifecycle(t2.id, "dormant", False)
    assert t2.state == "dormant"         # state still updates
    assert t2.suppressed is True         # untouched (human owns it)

    # missing id → silent no-op
    await r.set_lifecycle(2_000_000_666, "peak", True)


async def test_list_for_lifecycle_filters_orders_and_limits(db_ctx):
    s = db_ctx.session
    r = TrendRepo(s)
    b = f"{MARKER}_lfl"
    now = _now()
    t_recent = await _add_trend(s, brand=b, status="approved", last_seen_at=now)
    t_older = await _add_trend(s, brand=b, status="detected",
                               last_seen_at=now - timedelta(hours=5))
    t_null = await _add_trend(s, brand=b, status="completed", last_seen_at=None)
    await _add_trend(s, brand=b, status="dismissed", last_seen_at=now)  # excluded
    await _add_trend(s, brand=f"{MARKER}_lfl_other", status="approved",
                     last_seen_at=now)  # other brand — excluded

    rows = await r.list_for_lifecycle(b)
    ids = [t.id for t in rows]
    # monitor set only; dismissed + other-brand excluded
    assert set(ids) == {t_recent.id, t_older.id, t_null.id}
    # last_seen_at desc, NULLs last
    assert ids == [t_recent.id, t_older.id, t_null.id]

    limited = await r.list_for_lifecycle(b, limit=1)
    assert [t.id for t in limited] == [t_recent.id]


# =========================================================================== #
# PipelineRepo — reads
# =========================================================================== #
async def test_pipeline_get_hit_eager_loads_and_miss(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_pget"
    t = await _add_trend(s, brand=b)
    p = await _add_pipeline(s, brand=b, trend_id=t.id)
    j = await _add_job(s, pipeline_id=p.id)

    got = await pr.get(p.id)
    assert got is not None and got.id == p.id
    assert [jb.id for jb in got.jobs] == [j.id]
    assert got.trend is not None and got.trend.id == t.id
    assert [pl.id for pl in got.trend.pipelines] == [p.id]  # chained load

    assert await pr.get(2_000_000_777) is None


async def test_pipeline_list_brand_status_limit_and_order(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_plist"
    now = _now()
    p_old = await _add_pipeline(s, brand=b, status="pending_approval",
                                created_at=now - timedelta(hours=2))
    p_mid = await _add_pipeline(s, brand=b, status="approved",
                                created_at=now - timedelta(hours=1))
    p_new = await _add_pipeline(s, brand=b, status="declined", created_at=now)
    await _add_pipeline(s, brand=f"{MARKER}_plist_other", status="approved")

    rows = await pr.list(brand=b)
    assert [p.id for p in rows] == [p_new.id, p_mid.id, p_old.id]  # created_at desc

    approved = await pr.list(brand=b, statuses=["approved"])
    assert [p.id for p in approved] == [p_mid.id]

    assert [p.id for p in await pr.list(brand=b, limit=2)] == [p_new.id, p_mid.id]

    # no-filter call exercises the brand=None / statuses=None branches
    assert isinstance(await pr.list(limit=1), list)
    assert isinstance(await pr.list(statuses=["approved"], limit=1), list)


async def test_open_count_counts_only_open_statuses(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_open"
    for st in ("pending_approval", "approved", "generating", "previews_ready",
               "partially_published"):
        await _add_pipeline(s, brand=b, status=st)
    for st in ("declined", "closed", "expired", "failed", "published"):
        await _add_pipeline(s, brand=b, status=st)

    assert await pr.open_count(b) == 5
    assert await pr.open_count(f"{MARKER}_open_none_{uuid4().hex}") == 0


async def test_get_job_hit_eager_loads_and_miss(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_getjob"
    t = await _add_trend(s, brand=b)
    p = await _add_pipeline(s, brand=b, trend_id=t.id)
    j = await _add_job(s, pipeline_id=p.id)

    got = await pr.get_job(j.id)
    assert got is not None and got.id == j.id
    assert got.pipeline is not None and got.pipeline.id == p.id
    assert got.pipeline.trend is not None and got.pipeline.trend.id == t.id

    assert await pr.get_job(2_000_000_888) is None


# =========================================================================== #
# PipelineRepo.create
# =========================================================================== #
async def test_create_without_trend_validates_and_dedupes_content_types(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_create"
    p = await pr.create(trend_id=None, brand=b,
                        content_types=["Article", "article", "social_post"])
    assert p.status == "pending_approval"
    assert p.requested_by == "trend_scout"
    assert p.content_types == ["article", "social_post"]  # lowercased + deduped
    assert any(e.get("event") == "created" for e in (p.events or []))


async def test_create_with_trend_and_dup_guard(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_create_dup"
    t = await _add_trend(s, brand=b)
    p = await pr.create(trend_id=t.id, brand=b, content_types=["article"],
                        instructions="focus on X")
    assert p.trend_id == t.id and p.instructions == "focus on X"

    # a second open pipeline on the same trend is refused
    with pytest.raises(LifecycleError):
        await pr.create(trend_id=t.id, brand=b, content_types=["article"])


async def test_create_allows_new_pipeline_when_prior_is_terminal(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_create_term"
    t = await _add_trend(s, brand=b)
    await _add_pipeline(s, brand=b, trend_id=t.id, status="declined")  # terminal, not open
    p = await pr.create(trend_id=t.id, brand=b, content_types=["article"])
    assert p.status == "pending_approval"


async def test_create_rejects_bad_content_types(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_create_bad"
    with pytest.raises(LifecycleError):
        await pr.create(trend_id=None, brand=b, content_types=["bogus"])
    with pytest.raises(LifecycleError):
        await pr.create(trend_id=None, brand=b, content_types=[])


# =========================================================================== #
# PipelineRepo.approve / decline / close
# =========================================================================== #
async def test_approve_happy_with_overrides(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_approve"
    p = await _add_pipeline(s, brand=b, status="pending_approval",
                            content_types=["article"])
    out = await pr.approve(p.id, USER, content_types=["article", "social_post"],
                           instructions="  ship it  ")
    assert out.status == "approved"
    assert out.approved_by == USER and out.approved_at is not None
    assert out.content_types == ["article", "social_post"]
    assert out.instructions == "ship it"   # stripped
    assert any(e.get("event") == "approved" for e in (out.events or []))


async def test_approve_ignores_blank_instructions(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_approve_blank"
    p = await _add_pipeline(s, brand=b, status="pending_approval")
    out = await pr.approve(p.id, USER, instructions="   ")
    assert out.instructions is None   # whitespace-only → left untouched


async def test_approve_nonhuman_missing_and_invalid_transition(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_approve_err"
    p = await _add_pipeline(s, brand=b, status="pending_approval")
    with pytest.raises(LifecycleError):
        await pr.approve(p.id, "orchestrator")   # non-human
    with pytest.raises(LifecycleError):
        await pr.approve(2_000_000_999, USER)    # missing

    declined = await _add_pipeline(s, brand=b, status="declined")
    with pytest.raises(LifecycleError):
        await pr.approve(declined.id, USER)      # declined → approved invalid


async def test_decline_happy_nonhuman_and_missing(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_decline"
    p = await _add_pipeline(s, brand=b, status="pending_approval")
    out = await pr.decline(p.id, USER, reason="off-brand")
    assert out.status == "declined"
    assert out.declined_by == USER and out.declined_at is not None
    assert out.close_reason == "off-brand"
    assert any(e.get("event") == "declined" for e in (out.events or []))

    with pytest.raises(LifecycleError):
        await pr.decline(p.id, "scout")           # non-human (also already declined)
    with pytest.raises(LifecycleError):
        await pr.decline(2_000_001_000, USER)     # missing


async def test_close_cancels_live_jobs_only(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_close"
    p = await _add_pipeline(s, brand=b, status="approved")
    jq = await _add_job(s, pipeline_id=p.id, status="queued")
    jr = await _add_job(s, pipeline_id=p.id, status="running")
    jp = await _add_job(s, pipeline_id=p.id, status="published")
    jc = await _add_job(s, pipeline_id=p.id, status="cancelled")

    out = await pr.close(p.id, USER, reason="superseded")
    assert out.status == "closed"
    assert out.closed_at is not None and out.close_reason == "superseded"

    by_id = {j.id: j.status for j in out.jobs}
    assert by_id[jq.id] == "cancelled"    # live → cancelled
    assert by_id[jr.id] == "cancelled"    # live → cancelled
    assert by_id[jp.id] == "published"    # terminal record kept
    assert by_id[jc.id] == "cancelled"    # already cancelled, unchanged
    assert any(e.get("event") == "closed" for e in (out.events or []))


async def test_close_nonhuman_and_missing(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_close_err"
    p = await _add_pipeline(s, brand=b, status="approved")
    with pytest.raises(LifecycleError):
        await pr.close(p.id, "system")
    with pytest.raises(LifecycleError):
        await pr.close(2_000_001_100, USER)


# =========================================================================== #
# PipelineRepo.set_status
# =========================================================================== #
async def test_pipeline_set_status_valid_missing_and_invalid(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_psetstatus"
    p = await _add_pipeline(s, brand=b, status="pending_approval")
    await pr.set_status(p.id, "approved", actor="pipeline", detail="auto")
    assert p.status == "approved"
    assert any(e.get("event") == "approved" for e in (p.events or []))

    # missing → silent no-op
    await pr.set_status(2_000_001_200, "approved")

    # invalid transition raises
    p2 = await _add_pipeline(s, brand=b, status="pending_approval")
    with pytest.raises(LifecycleError):
        await pr.set_status(p2.id, "published")   # pending_approval → published invalid


# =========================================================================== #
# PipelineRepo.refresh_rollup
# =========================================================================== #
async def test_refresh_rollup_no_jobs_returns_current(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_rollup_nojobs"
    p = await _add_pipeline(s, brand=b, status="approved")
    assert await pr.refresh_rollup(p.id) == "approved"
    assert p.status == "approved"


async def test_refresh_rollup_missing_returns_empty(db_ctx):
    pr = PipelineRepo(db_ctx.session)
    assert await pr.refresh_rollup(2_000_001_300) == ""


async def test_refresh_rollup_terminal_pipeline_unchanged(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_rollup_term"
    p = await _add_pipeline(s, brand=b, status="closed")
    await _add_job(s, pipeline_id=p.id, status="queued")
    assert await pr.refresh_rollup(p.id) == "closed"   # terminal → no derivation


async def test_refresh_rollup_to_generating(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_rollup_gen"
    p = await _add_pipeline(s, brand=b, status="approved")
    await _add_job(s, pipeline_id=p.id, status="queued")
    assert await pr.refresh_rollup(p.id) == "generating"
    assert p.status == "generating"


async def test_refresh_rollup_to_previews_ready(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_rollup_prev"
    p = await _add_pipeline(s, brand=b, status="generating")
    await _add_job(s, pipeline_id=p.id, status="preview_ready")
    assert await pr.refresh_rollup(p.id) == "previews_ready"


async def test_refresh_rollup_to_published(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_rollup_pub"
    p = await _add_pipeline(s, brand=b, status="previews_ready")
    await _add_job(s, pipeline_id=p.id, status="published")
    await _add_job(s, pipeline_id=p.id, status="published")
    assert await pr.refresh_rollup(p.id) == "published"


async def test_refresh_rollup_to_partially_published(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_rollup_part"
    p = await _add_pipeline(s, brand=b, status="previews_ready")
    await _add_job(s, pipeline_id=p.id, status="published")
    await _add_job(s, pipeline_id=p.id, status="rejected")
    assert await pr.refresh_rollup(p.id) == "partially_published"


async def test_refresh_rollup_all_rejected_closes(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_rollup_closed"
    p = await _add_pipeline(s, brand=b, status="previews_ready")
    await _add_job(s, pipeline_id=p.id, status="rejected")
    await _add_job(s, pipeline_id=p.id, status="rejected")
    assert await pr.refresh_rollup(p.id) == "closed"
    assert p.closed_at is not None
    assert p.close_reason == "all previews rejected"


async def test_refresh_rollup_all_failed(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_rollup_failed"
    p = await _add_pipeline(s, brand=b, status="generating")
    await _add_job(s, pipeline_id=p.id, status="failed")
    await _add_job(s, pipeline_id=p.id, status="failed")
    assert await pr.refresh_rollup(p.id) == "failed"


async def test_refresh_rollup_swallows_invalid_transition(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_rollup_invalid"
    # pending_approval + a queued job derives "generating", but that transition
    # is invalid from pending_approval → the status is left unchanged.
    p = await _add_pipeline(s, brand=b, status="pending_approval")
    await _add_job(s, pipeline_id=p.id, status="queued")
    assert await pr.refresh_rollup(p.id) == "pending_approval"
    assert p.status == "pending_approval"


# =========================================================================== #
# PipelineRepo.add_job / mark_job / review_job / regenerate_job
# =========================================================================== #
async def test_add_job_creates_queued_job(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_addjob"
    p = await _add_pipeline(s, brand=b, status="approved")
    j = await pr.add_job(p, content_type="social_post", transport="llm",
                         instructions="be punchy", persona_id=7)
    assert j.pipeline_id == p.id
    assert j.status == "queued"
    assert j.content_type == "social_post"
    assert j.transport == "llm"
    assert j.instructions == "be punchy"
    assert j.persona_id == 7
    assert j.attempt == 1


async def test_mark_job_happy_missing_and_invalid(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_markjob"
    p = await _add_pipeline(s, brand=b, status="generating")
    j = await _add_job(s, pipeline_id=p.id, status="queued")

    running = await pr.mark_job(j.id, "running")
    assert running.status == "running" and running.updated_at is not None

    # extra kwargs are written straight through onto the job
    ready = await pr.mark_job(j.id, "preview_ready", preview_ref={"art": 1},
                              preview_meta={"word_count": 900})
    assert ready.status == "preview_ready"
    assert ready.preview_ref == {"art": 1}
    assert ready.preview_meta == {"word_count": 900}

    # missing job
    with pytest.raises(LifecycleError):
        await pr.mark_job(2_000_001_400, "running")

    # invalid transition (preview_ready → running not allowed)
    with pytest.raises(LifecycleError):
        await pr.mark_job(j.id, "running")


async def test_review_job_approve_and_reject(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_review"
    p = await _add_pipeline(s, brand=b, status="previews_ready")
    j1 = await _add_job(s, pipeline_id=p.id, status="preview_ready")
    j2 = await _add_job(s, pipeline_id=p.id, status="preview_ready")

    approved = await pr.review_job(j1.id, USER, approve=True)
    assert approved.status == "approved"
    assert approved.reviewed_by == USER and approved.reviewed_at is not None

    rejected = await pr.review_job(j2.id, USER, approve=False)
    assert rejected.status == "rejected"

    events = [e.get("event") for e in (p.events or [])]
    assert "preview_approved" in events
    assert "preview_rejected" in events


async def test_review_job_nonhuman_missing_and_closed_pipeline(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_review_err"
    p = await _add_pipeline(s, brand=b, status="previews_ready")
    j = await _add_job(s, pipeline_id=p.id, status="preview_ready")

    with pytest.raises(LifecycleError):
        await pr.review_job(j.id, "trend_scout", approve=True)   # non-human
    with pytest.raises(LifecycleError):
        await pr.review_job(2_000_001_500, USER, approve=True)   # missing

    # a closed pipeline blocks review (require_recoverable_pipeline)
    closed = await _add_pipeline(s, brand=b, status="closed")
    jc = await _add_job(s, pipeline_id=closed.id, status="preview_ready")
    with pytest.raises(LifecycleError):
        await pr.review_job(jc.id, USER, approve=True)


async def test_regenerate_job_archives_and_requeues(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_regen"
    p = await _add_pipeline(s, brand=b, status="previews_ready")
    j = await _add_job(s, pipeline_id=p.id, status="preview_ready",
                       instructions="v1", attempt=1,
                       preview_ref={"a": 1}, preview_meta={"w": 10}, error="prev err")

    out = await pr.regenerate_job(j.id, USER, "tighten the intro")
    assert out.status == "queued"
    assert out.attempt == 2
    assert out.instructions == "tighten the intro"
    assert out.preview_ref is None and out.preview_meta is None and out.error is None
    # prior attempt archived
    assert out.history and out.history[-1]["attempt"] == 1
    assert out.history[-1]["instructions"] == "v1"
    assert any(e.get("event") == "regenerate_requested" for e in (p.events or []))


async def test_regenerate_job_blank_instructions_keep_previous(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_regen_blank"
    p = await _add_pipeline(s, brand=b, status="previews_ready")
    j = await _add_job(s, pipeline_id=p.id, status="preview_ready", instructions="keep me")
    out = await pr.regenerate_job(j.id, USER, "   ")
    assert out.status == "queued"
    assert out.instructions == "keep me"   # blank → previous retained


async def test_regenerate_job_nonhuman_missing_and_invalid_state(db_ctx):
    s = db_ctx.session
    pr = PipelineRepo(s)
    b = f"{MARKER}_regen_err"
    p = await _add_pipeline(s, brand=b, status="generating")
    with pytest.raises(LifecycleError):
        await pr.regenerate_job(2_000_001_600, USER, "x")   # missing (before actor? actor first)
    with pytest.raises(LifecycleError):
        await pr.regenerate_job(2_000_001_600, "scout", "x")  # non-human

    # a queued job cannot be regenerated (queued → queued is not a valid transition)
    j = await _add_job(s, pipeline_id=p.id, status="queued")
    with pytest.raises(LifecycleError):
        await pr.regenerate_job(j.id, USER, "retry")


# =========================================================================== #
# PipelineRepo — raw-SQL claim/reap  (global mutation → rollback session)
# =========================================================================== #
async def test_claim_queued_flips_queued_to_running():
    async with _rb_session() as s:
        pr = PipelineRepo(s)
        b = f"{MARKER}_claimq"
        p = await _add_pipeline(s, brand=b, status="approved")
        j1 = await _add_job(s, pipeline_id=p.id, status="queued")
        j2 = await _add_job(s, pipeline_id=p.id, status="queued")

        claimed = await pr.claim_queued(limit=100_000)  # big → guarantees mine included
        assert j1.id in claimed and j2.id in claimed
        statuses = (await s.execute(
            select(ContentJob.status).where(ContentJob.id.in_([j1.id, j2.id])))).scalars().all()
        assert set(statuses) == {"running"}


async def test_claim_stuck_running_limit_guard_and_selection():
    async with _rb_session() as s:
        pr = PipelineRepo(s)
        # non-positive limit → short-circuits to [] (no query)
        assert await pr.claim_stuck_running(_now(), limit=0) == []
        assert await pr.claim_stuck_running(_now(), limit=-3) == []

        b = f"{MARKER}_claimsr"
        p = await _add_pipeline(s, brand=b, status="generating")
        old = _now() - timedelta(hours=1)
        stuck = await _add_job(s, pipeline_id=p.id, status="running",
                               external_ref={"topic_id": 1}, updated_at=old)
        # running but no external_ref → NOT claimed by this sweep. NB: external_ref
        # is OMITTED (not None) — a JSONB column set to Python None persists a JSON
        # `null`, which is NOT SQL NULL, so `external_ref IS NOT NULL` would match it.
        no_ext = await _add_job(s, pipeline_id=p.id, status="running", updated_at=old)

        claimed = await pr.claim_stuck_running(_now(), limit=100_000)
        assert stuck.id in claimed
        assert no_ext.id not in claimed
        # updated_at was bumped forward
        bumped = (await s.execute(
            select(ContentJob.updated_at).where(ContentJob.id == stuck.id))).scalar_one()
        assert bumped > old


async def test_reap_dead_running_fails_orphans_and_rolls_up():
    async with _rb_session() as s:
        pr = PipelineRepo(s)
        b = f"{MARKER}_reap"
        p = await _add_pipeline(s, brand=b, status="generating")
        old = _now() - timedelta(hours=1)
        # external_ref OMITTED → real SQL NULL (Python None would persist JSON
        # `null`, which is NOT SQL NULL and would dodge the `IS NULL` reap filter).
        d1 = await _add_job(s, pipeline_id=p.id, status="running", updated_at=old)
        d2 = await _add_job(s, pipeline_id=p.id, status="running", updated_at=old)
        # has an external pipeline to resume → must NOT be reaped
        keep = await _add_job(s, pipeline_id=p.id, status="running",
                              external_ref={"topic_id": 9}, updated_at=old)
        # capture ids before expiring (expired attrs would need a greenlet to reload)
        d1_id, d2_id, keep_id, p_id = d1.id, d2.id, keep.id, p.id

        # Emulate a fresh session (production): drop in-memory state so the
        # rollup inside reap re-reads the raw-updated job rows.
        s.expire_all()

        n = await pr.reap_dead_running(_now())
        assert n >= 2

        rows = dict((await s.execute(
            select(ContentJob.id, ContentJob.status)
            .where(ContentJob.id.in_([d1_id, d2_id, keep_id])))).all())
        assert rows[d1_id] == "failed"
        assert rows[d2_id] == "failed"
        assert rows[keep_id] == "running"

        err = (await s.execute(
            select(ContentJob.error).where(ContentJob.id == d1_id))).scalar_one()
        assert err and "worker died" in err

        # pipeline still had a live (keep) job → rollup keeps it generating
        pst = (await s.execute(
            select(ContentPipeline.status).where(ContentPipeline.id == p_id))).scalar_one()
        assert pst == "generating"


# =========================================================================== #
# job mutations on a pipeline-less job (defensive `job.pipeline is None` path).
# A NULL pipeline_id job cannot be marker-cleaned (no brand) → rollback session.
# =========================================================================== #
async def test_review_job_without_pipeline():
    async with _rb_session() as s:
        pr = PipelineRepo(s)
        j = await _add_job(s, pipeline_id=None, status="preview_ready")
        out = await pr.review_job(j.id, USER, approve=True)
        assert out.status == "approved"
        assert out.reviewed_by == USER and out.reviewed_at is not None
        assert out.pipeline is None   # no pipeline event was logged, no crash


async def test_regenerate_job_without_pipeline():
    async with _rb_session() as s:
        pr = PipelineRepo(s)
        j = await _add_job(s, pipeline_id=None, status="rejected",
                           instructions="old", attempt=1)
        out = await pr.regenerate_job(j.id, USER, "new guidance")
        assert out.status == "queued"
        assert out.attempt == 2
        assert out.instructions == "new guidance"
