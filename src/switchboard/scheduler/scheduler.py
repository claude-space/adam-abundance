"""APScheduler wiring for the morning cycle, feeders, and the TTL sweep.

All jobs run on America/New_York (the portfolio's cadence). The morning cycle
stops at a draft plan; feeders only write to memory. Times are config-overridable
but default to: feeders early, then the cycle, with a content-audit refresh
midday and an hourly TTL sweep.
"""

from __future__ import annotations

import asyncio

from ..config import get_settings
from ..context import RunContext
from ..feeders import run_feeder
from ..logging_ import get_logger
from ..orchestrator import run_morning_cycle

log = get_logger("scheduler")
_TZ = "America/New_York"


async def _sweep() -> None:
    async with RunContext.open() as ctx:
        await ctx.store.expire_stale()


async def _supersede() -> None:
    async with RunContext.open() as ctx:
        await ctx.store.supersede_duplicates()


async def _pipeline_jobs() -> None:
    """Cross-process fallback worker for queued/stuck content jobs."""
    from ..trends.pipeline import run_job_sweep  # lazy: optional deps

    await run_job_sweep()


async def _trend_expire() -> None:
    """Perishability must not depend on scans succeeding: expire unactioned
    trends (+ their pending trigger requests) on a fixed cadence too."""
    from ..trends.repo import TrendRepo  # lazy: optional deps

    async with RunContext.open() as ctx:
        await TrendRepo(ctx.session).expire_stale()


def build_scheduler():
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore
        from apscheduler.triggers.cron import CronTrigger  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("APScheduler not installed") from exc

    settings = get_settings()
    sched = AsyncIOScheduler(timezone=_TZ)

    for brand in settings.brand_keys:
        # Feeders first (early morning), then the synthesis cycle.
        sched.add_job(run_feeder, CronTrigger(hour=6, minute=0, timezone=_TZ),
                      args=["decay", brand], id=f"decay:{brand}", replace_existing=True)
        sched.add_job(run_morning_cycle, CronTrigger(hour=7, minute=30, timezone=_TZ),
                      args=[brand], id=f"cycle:{brand}", replace_existing=True)
        # Content-audit findings refresh twice during the day.
        sched.add_job(run_feeder, CronTrigger(hour="9,14", minute=5, timezone=_TZ),
                      args=["content_audit", brand], id=f"audit:{brand}", replace_existing=True)

    sched.add_job(_sweep, CronTrigger(minute=15, timezone=_TZ), id="ttl_sweep", replace_existing=True)
    sched.add_job(_supersede, CronTrigger(hour=5, minute=45, timezone=_TZ),
                  id="supersede_sweep", replace_existing=True)

    # Competitor trend pipeline (docs/trend-pipeline.md): one portfolio-wide scan
    # on its own cadence + the content-job worker sweep. Both no-op when
    # TREND_PIPELINE_ENABLED=0 (the scan checks it; the sweep just finds no jobs).
    if settings.trends.enabled:
        from apscheduler.triggers.interval import IntervalTrigger  # type: ignore

        sched.add_job(run_feeder, IntervalTrigger(minutes=settings.trends.scan_interval_min,
                                                  timezone=_TZ),
                      args=["trend_scan", "portfolio"], id="trend_scan:portfolio",
                      replace_existing=True)
        sched.add_job(_pipeline_jobs, IntervalTrigger(minutes=2, timezone=_TZ),
                      id="pipeline_jobs", replace_existing=True)
    # Runs even when scans are disabled, so proposed trends still expire.
    sched.add_job(_trend_expire, CronTrigger(minute=25, timezone=_TZ),
                  id="trend_expire", replace_existing=True)
    return sched


async def run_scheduler() -> int:
    settings = get_settings()
    sched = build_scheduler()
    sched.start()
    jobs = sched.get_jobs()
    log.info("Scheduler started (%s) with %d jobs: %s", _TZ, len(jobs), [j.id for j in jobs])
    print(f"Switchboard scheduler running ({_TZ}). Brands: {list(settings.brand_keys)}. Ctrl-C to stop.")
    for j in jobs:
        print(f"  · {j.id}: next @ {j.next_run_time}")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover
        log.info("Scheduler stopping")
        sched.shutdown(wait=False)
    return 0
