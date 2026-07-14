"""Scheduled feeders (PRD §6 "Scheduled feeders (NOT agents)").

The ranking-decay scan and the content-depth auditor run on their existing
schedules and drop candidates/findings into shared memory as typed entries. They
are gray boxes, not agents: they have no plan and own no action — they only
feed memory, which the owning agents (Analytics/Opportunity/Production) then
read and the orchestrator folds into the plan.
"""

from ..context import RunContext
from .content_audit import ContentAuditFeeder
from .decay import DecayScanFeeder
from .trend_scan import TrendScanFeeder

_FEEDERS = {"decay": DecayScanFeeder, "content_audit": ContentAuditFeeder,
            "trend_scan": TrendScanFeeder}


def build_feeder(name: str, ctx: RunContext):
    if name not in _FEEDERS:
        raise KeyError(f"Unknown feeder '{name}'")
    return _FEEDERS[name](ctx)


async def run_feeder(name: str, brand: str) -> int:
    async with RunContext.open() as ctx:
        return await build_feeder(name, ctx).run(brand)


__all__ = ["DecayScanFeeder", "ContentAuditFeeder", "TrendScanFeeder", "build_feeder", "run_feeder"]
