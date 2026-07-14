"""Trend-scan feeder (docs/trend-pipeline.md). Wraps the Trend Scout in the
standard feeder shape so it slots into the scheduler/CLI like decay and
content_audit. Like every feeder it only feeds — the scout writes signals,
trends, and *pending* trigger requests; humans approve them in the console."""

from __future__ import annotations

from ..context import RunContext
from ..logging_ import get_logger

log = get_logger("feeder.trend_scan")


class TrendScanFeeder:
    name = "trend_scan"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    async def run(self, brand: str) -> int:
        from ..trends.scout import TrendScout  # lazy: pulls optional client deps

        try:
            summary = await TrendScout(self.ctx).scan(brand)
        except Exception as exc:  # noqa: BLE001 — feeders degrade softly
            log.info("[trend_scan] scan failed: %s", exc)
            return 0
        return int(summary.get("new_trends", 0)) + int(summary.get("updated_trends", 0))
