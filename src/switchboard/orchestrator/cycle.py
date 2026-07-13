"""The morning cycle (PRD §6.1): observe → synthesize draft plan → brief.

It deliberately stops at a **draft** plan and a Slack/logged brief. It never
approves or dispatches — a human does that via the approval surface. Dispatch is
a separate, explicitly-invoked step (:mod:`switchboard.orchestrator.dispatch`).
"""

from __future__ import annotations

from datetime import date

from ..agents import run_all_observe
from ..context import RunContext
from ..logging_ import get_logger
from .planner import Planner
from .slack import post_brief

log = get_logger("orchestrator.cycle")


async def run_morning_cycle(brand: str, plan_date: date | None = None) -> int:
    plan_date = plan_date or date.today()
    log.info("=== morning cycle: %s %s ===", brand, plan_date.isoformat())

    # Housekeeping: expire stale memory before synthesizing.
    async with RunContext.open() as ctx:
        await ctx.store.expire_stale()

    # 1) Observe — every agent populates memory (each in its own transaction).
    obs = await run_all_observe(brand)
    log.info("observe results: %s", obs)

    # 2) Synthesize a draft plan + brief, and 3) surface the brief.
    async with RunContext.open() as ctx:
        planner = Planner(ctx)
        plan_id, brief = await planner.plan(brand, plan_date)
        await post_brief(ctx, brand, brief)

    print(f"Draft plan #{plan_id} created for {brand} ({plan_date.isoformat()}).")
    print("Review and approve in the dashboard (switchboard serve) — nothing dispatches without approval.")
    print("\n--- brief ---\n" + brief)
    failed = [a for a, r in obs.items() if r != "ok"]
    if failed:
        print(f"\n(observe warnings: {failed})")
    return 0
