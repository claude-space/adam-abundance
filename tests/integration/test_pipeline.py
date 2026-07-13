"""Integration tests against a real Postgres (PRD Phase 0/3/4 acceptance).

Run with a reachable DB, e.g.:
    DATABASE_URL=postgresql+asyncpg://switchboard:switchboard@localhost:5544/switchboard \
        PYTHONPATH=src pytest tests/integration -q
If no DB is reachable the whole module is skipped.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from switchboard.context import RunContext
from switchboard.db.enums import EntryType, PlanItemStatus, PlanStatus
from switchboard.interfaces import EntryDraft
from switchboard.orchestrator.dispatch import Dispatcher, DispatchError
from switchboard.orchestrator.plans import ApprovalError, PlanRepo

USER = "andrew.marks@valnetinc.com"


@pytest.fixture
async def ctx():
    try:
        async with RunContext.open() as c:
            await c.store.expire_stale()  # cheap connectivity ping
            yield c
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no reachable Postgres: {exc}")


async def test_memory_roundtrip(ctx):
    e = await ctx.store.write(EntryDraft(type=EntryType.METRIC, brand="hotcars", source_agent="test",
                                         source_system="itest", payload={"kind": "rt", "v": 1}))
    rows = await ctx.store.query(brand="hotcars", types=[EntryType.METRIC], source_system="itest", limit=5)
    assert any(r.id == e.id for r in rows)


async def test_fact_gate_downgrade(ctx):
    e = await ctx.store.write(EntryDraft(type=EntryType.FACT, brand="hotcars", source_agent="test",
                                         source_system="itest", payload={"statement": "x"}, verified=True))
    assert e.type == EntryType.CLAIM and e.verified is False
    # With the gate cleared (Research authority), it stays a verified fact.
    e2 = await ctx.store.write(EntryDraft(type=EntryType.FACT, brand="hotcars", source_agent="research",
                                          source_system="web_search", payload={"statement": "y"},
                                          verified=True), fact_gate_ok=True)
    assert e2.type == EntryType.FACT and e2.verified is True


async def test_ttl_sweep(ctx):
    e = await ctx.store.write(EntryDraft(type=EntryType.CONTEXT, brand="hotcars", source_agent="test",
                                         source_system="itest", payload={"kind": "expired"},
                                         expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    swept = await ctx.store.expire_stale()
    await ctx.session.refresh(e)
    assert swept >= 1 and e.status == "expired"


async def test_governor_charge_and_caps(ctx):
    before = await ctx.governor.spent_today("llm_micros")
    await ctx.governor.charge("llm_micros", 123, "test")
    after = await ctx.governor.spent_today("llm_micros")
    assert after - before == 123
    assert await ctx.governor.within_caps("llm_micros", additional=1) is True


async def test_approve_and_dryrun_dispatch():
    # Build a tiny plan with one dry-run notify item, then approve + dispatch.
    async with RunContext.open() as c:
        repo = PlanRepo(c.session)
        plan = await repo.create_plan("hotcars", date.today())
        await repo.add_item(plan, rank=1, assigned_agent="orchestrator", action_type="notify",
                            params={"message": "itest"}, rationale="integration test", dry_run=True)
        plan_id = plan.id

    # dispatch before approval → refused
    with pytest.raises(DispatchError):
        async with RunContext.open() as c:
            await Dispatcher(c).dispatch_plan(plan_id)

    # orchestrator cannot self-approve
    with pytest.raises(ApprovalError):
        async with RunContext.open() as c:
            await PlanRepo(c.session).approve_plan(plan_id, "orchestrator")

    # human approves plan + item (dry-run)
    async with RunContext.open() as c:
        repo = PlanRepo(c.session)
        await repo.approve_plan(plan_id, USER)
        p = await repo.get_plan(plan_id)
        await repo.approve_item(p.items[0].id, USER, go_live=False)

    # dispatch → item done, dry-run
    async with RunContext.open() as c:
        summary = await Dispatcher(c).dispatch_plan(plan_id)
    assert summary["done"] == 1 and summary["failed"] == 0 and summary["refused"] == 0

    async with RunContext.open() as c:
        p = await PlanRepo(c.session).get_plan(plan_id)
        assert p.status == PlanStatus.DISPATCHED.value
        assert p.items[0].status == PlanItemStatus.DONE.value
