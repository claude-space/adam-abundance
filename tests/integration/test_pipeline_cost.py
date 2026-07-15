"""Phase 10b (§16.4): the pipeline_cost rollup. Exercises the cost helper
directly against Postgres — construct a pipeline + jobs carrying metered LLM
micros, roll it up, and assert the USD math, idempotency, and the human-baseline
lookup. Uses a synthetic brand so it never collides with real portfolio data,
and cleans up after itself. Skips if no DB is reachable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from switchboard.context import RunContext
from switchboard.db.models import (
    ContentJob,
    ContentPipeline,
    PipelineCost,
    WriterPayBaseline,
)
from switchboard.trends.pipeline import _human_equiv_usd, _record_pipeline_cost

BRAND = "itest_costs"   # synthetic — never a real brand short_code


@pytest.fixture
async def ctx():
    try:
        async with RunContext.open() as c:
            await c.store.expire_stale()  # cheap connectivity ping
            yield c
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no reachable Postgres: {exc}")


async def _cleanup(session) -> None:
    await session.execute(delete(WriterPayBaseline).where(WriterPayBaseline.brand == BRAND))
    await session.execute(delete(PipelineCost).where(PipelineCost.brand == BRAND))
    await session.execute(delete(ContentPipeline).where(ContentPipeline.brand == BRAND))
    await session.flush()


async def _make_pipeline(session) -> ContentPipeline:
    pipeline = ContentPipeline(brand=BRAND, status="published", requested_by="test",
                              content_types=["article", "social_post"])
    session.add(pipeline)
    await session.flush()
    # 250k + 750k micros = exactly $1.00; 400 + 600 = 1000 words.
    session.add(ContentJob(pipeline_id=pipeline.id, content_type="article", status="published",
                           cost={"llm_micros": 250_000}, preview_meta={"word_count": 400}))
    session.add(ContentJob(pipeline_id=pipeline.id, content_type="social_post", status="published",
                           cost={"llm_micros": 750_000}, preview_meta={"word_count": 600}))
    await session.flush()
    from switchboard.trends.repo import PipelineRepo
    return await PipelineRepo(session).get(pipeline.id)   # reload with .jobs


async def test_pipeline_cost_rollup_and_idempotency(ctx):
    await _cleanup(ctx.session)
    pipeline = await _make_pipeline(ctx.session)
    run_id = f"pipeline:{pipeline.id}"

    # First rollup: LLM-only, no baseline → human/savings stay NULL.
    await _record_pipeline_cost(ctx, pipeline)
    rows = (await ctx.session.execute(
        select(PipelineCost).where(PipelineCost.pipeline_run_id == run_id))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.total_usd == pytest.approx(1.0)
    assert row.cost_breakdown == {"llm_usd": 1.0, "ahrefs_usd": 0.0, "bq_usd": 0.0, "other_usd": 0.0}
    assert row.human_equiv_usd is None and row.savings_usd is None
    assert row.used_style_profile is False

    # Second rollup (e.g. PARTIALLY_PUBLISHED → PUBLISHED) upserts, never duplicates.
    await _record_pipeline_cost(ctx, pipeline)
    rows = (await ctx.session.execute(
        select(PipelineCost).where(PipelineCost.pipeline_run_id == run_id))).scalars().all()
    assert len(rows) == 1

    # Add a brand pay baseline → the human comparison now fills in.
    ctx.session.add(WriterPayBaseline(brand=BRAND, author=None, usd_per_article=45.0))
    await ctx.session.flush()
    await _record_pipeline_cost(ctx, pipeline)
    await ctx.session.refresh(rows[0])
    assert rows[0].human_equiv_usd == pytest.approx(45.0)
    assert rows[0].savings_usd == pytest.approx(44.0)   # 45 - 1

    await _cleanup(ctx.session)


async def test_human_equiv_baseline_lookup(ctx):
    await _cleanup(ctx.session)
    # No baseline → None (panel shows "awaiting rates").
    assert await _human_equiv_usd(ctx.session, BRAND, 1000) is None

    # Per-word fallback when no flat per-article rate is set: 0.05 × 1000 = 50.
    ctx.session.add(WriterPayBaseline(brand=BRAND, author=None, usd_per_word=0.05))
    await ctx.session.flush()
    assert await _human_equiv_usd(ctx.session, BRAND, 1000) == pytest.approx(50.0)
    # Zero words with a per-word-only rate can't be priced → None.
    assert await _human_equiv_usd(ctx.session, BRAND, 0) is None

    await _cleanup(ctx.session)
