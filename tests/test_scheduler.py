"""Unit tests for the APScheduler wiring (PRD §11 scheduling plane).

APScheduler is an OPTIONAL runtime dep and is not installed in the unit-test
environment, so:

* ``build_scheduler`` genuinely raises ``RuntimeError`` here (asserted directly),
  and
* the job-registration/interval logic is exercised against a FAKE apscheduler
  module tree injected into ``sys.modules`` — a fake ``AsyncIOScheduler`` records
  every ``add_job`` and fake ``CronTrigger``/``IntervalTrigger`` capture their
  kwargs. No real scheduler, no event-loop timers, no DB.

``run_scheduler``'s otherwise-infinite ``await asyncio.sleep(3600)`` loop is
driven exactly one tick by swapping the module's ``asyncio`` for a stub whose
``sleep`` raises ``CancelledError`` (the loop's own stop path).
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import switchboard.scheduler as sched_pkg
import switchboard.scheduler.scheduler as sched_mod
from switchboard.scheduler import build_scheduler, run_scheduler

_TZ = "America/New_York"


# ---------------------------------------------------------------------------
# Fake apscheduler
# ---------------------------------------------------------------------------


class FakeJob:
    def __init__(self, func, trigger, args, id):
        self.func = func
        self.trigger = trigger
        self.args = args
        self.id = id
        self.next_run_time = None


class FakeScheduler:
    def __init__(self, timezone=None):
        self.timezone = timezone
        self.jobs: dict[str, FakeJob] = {}
        self.started = False
        self.shutdown_wait = "not-called"

    def add_job(self, func, trigger, args=None, id=None, replace_existing=False, **kw):
        job = FakeJob(func, trigger, args, id)
        self.jobs[id] = job
        return job

    def get_jobs(self):
        return list(self.jobs.values())

    def start(self):
        self.started = True

    def shutdown(self, wait=True):
        self.shutdown_wait = wait


class FakeCron:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeInterval:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _install_fake_apscheduler(monkeypatch):
    ap = types.ModuleType("apscheduler")
    sp = types.ModuleType("apscheduler.schedulers")
    sa = types.ModuleType("apscheduler.schedulers.asyncio")
    tp = types.ModuleType("apscheduler.triggers")
    tc = types.ModuleType("apscheduler.triggers.cron")
    ti = types.ModuleType("apscheduler.triggers.interval")
    sa.AsyncIOScheduler = FakeScheduler
    tc.CronTrigger = FakeCron
    ti.IntervalTrigger = FakeInterval
    ap.schedulers, ap.triggers = sp, tp
    sp.asyncio = sa
    tp.cron, tp.interval = tc, ti
    for name, mod in [
        ("apscheduler", ap),
        ("apscheduler.schedulers", sp),
        ("apscheduler.schedulers.asyncio", sa),
        ("apscheduler.triggers", tp),
        ("apscheduler.triggers.cron", tc),
        ("apscheduler.triggers.interval", ti),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


def _fake_settings(*, brands=("hotcars", "carbuzz", "topspeed"), enabled=True, interval=120):
    return SimpleNamespace(
        brand_keys=brands,
        trends=SimpleNamespace(enabled=enabled, scan_interval_min=interval),
    )


def _patch_settings(monkeypatch, **kw):
    monkeypatch.setattr(sched_mod, "get_settings", lambda: _fake_settings(**kw))


# ---------------------------------------------------------------------------
# __init__ exports
# ---------------------------------------------------------------------------


def test_package_exports():
    assert sched_pkg.build_scheduler is build_scheduler
    assert sched_pkg.run_scheduler is run_scheduler
    assert set(sched_pkg.__all__) == {"build_scheduler", "run_scheduler"}


# ---------------------------------------------------------------------------
# build_scheduler — absence of apscheduler (actual env behavior)
# ---------------------------------------------------------------------------


def test_build_scheduler_raises_when_apscheduler_absent(monkeypatch):
    # This path is only reachable when APScheduler genuinely isn't importable
    # (the case in this local venv). In CI `pip install -e .` DOES install it
    # (it's a base dep), so deleting the sys.modules cache would just re-import
    # it successfully — skip there rather than assert a raise that won't happen.
    try:
        import apscheduler  # noqa: F401

        pytest.skip("APScheduler is installed; cannot exercise the missing-dep path")
    except ImportError:
        pass
    for name in list(sys.modules):
        if name == "apscheduler" or name.startswith("apscheduler."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    with pytest.raises(RuntimeError, match="APScheduler not installed"):
        build_scheduler()


# ---------------------------------------------------------------------------
# build_scheduler — job registration (fake apscheduler)
# ---------------------------------------------------------------------------


def test_registers_full_job_set_when_trends_enabled(monkeypatch):
    _install_fake_apscheduler(monkeypatch)
    _patch_settings(monkeypatch, brands=("hotcars", "carbuzz", "topspeed"), enabled=True)
    sched = build_scheduler()

    assert isinstance(sched, FakeScheduler)
    assert sched.timezone == _TZ
    ids = set(sched.jobs)
    for b in ("hotcars", "carbuzz", "topspeed"):
        assert {f"decay:{b}", f"cycle:{b}", f"audit:{b}"} <= ids
    assert {"ttl_sweep", "supersede_sweep", "trend_expire",
            "trend_scan:portfolio", "pipeline_jobs"} <= ids
    # 3 brands x 3 per-brand jobs + 5 portfolio/global jobs
    assert len(sched.jobs) == 3 * 3 + 5


def test_trends_disabled_skips_scan_and_pipeline_but_keeps_expire(monkeypatch):
    _install_fake_apscheduler(monkeypatch)
    _patch_settings(monkeypatch, brands=("hotcars",), enabled=False)
    sched = build_scheduler()
    ids = set(sched.jobs)

    assert "trend_scan:portfolio" not in ids
    assert "pipeline_jobs" not in ids
    assert "trend_expire" in ids  # perishability sweep runs even when scans are off
    assert {"decay:hotcars", "cycle:hotcars", "audit:hotcars"} <= ids
    # 1 brand x 3 + (ttl_sweep, supersede_sweep, trend_expire)
    assert len(sched.jobs) == 3 * 1 + 3


def test_job_funcs_args_and_triggers(monkeypatch):
    _install_fake_apscheduler(monkeypatch)
    _patch_settings(monkeypatch, brands=("hotcars",), enabled=True, interval=90)
    j = build_scheduler().jobs

    # funcs + args
    assert j["decay:hotcars"].func is sched_mod.run_feeder
    assert j["decay:hotcars"].args == ["decay", "hotcars"]
    assert j["cycle:hotcars"].func is sched_mod.run_morning_cycle
    assert j["cycle:hotcars"].args == ["hotcars"]
    assert j["audit:hotcars"].func is sched_mod.run_feeder
    assert j["audit:hotcars"].args == ["content_audit", "hotcars"]
    assert j["trend_scan:portfolio"].func is sched_mod.run_feeder
    assert j["trend_scan:portfolio"].args == ["trend_scan", "portfolio"]
    assert j["ttl_sweep"].func is sched_mod._sweep
    assert j["supersede_sweep"].func is sched_mod._supersede
    assert j["trend_expire"].func is sched_mod._trend_expire
    assert j["pipeline_jobs"].func is sched_mod._pipeline_jobs

    # cron fields
    assert isinstance(j["decay:hotcars"].trigger, FakeCron)
    assert j["decay:hotcars"].trigger.kwargs == {"hour": 6, "minute": 0, "timezone": _TZ}
    assert j["cycle:hotcars"].trigger.kwargs == {"hour": 7, "minute": 30, "timezone": _TZ}
    assert j["audit:hotcars"].trigger.kwargs == {"hour": "9,14", "minute": 5, "timezone": _TZ}
    assert j["ttl_sweep"].trigger.kwargs == {"minute": 15, "timezone": _TZ}
    assert j["supersede_sweep"].trigger.kwargs == {"hour": 5, "minute": 45, "timezone": _TZ}
    assert j["trend_expire"].trigger.kwargs == {"minute": 25, "timezone": _TZ}

    # interval triggers
    assert isinstance(j["trend_scan:portfolio"].trigger, FakeInterval)
    assert j["trend_scan:portfolio"].trigger.kwargs == {"minutes": 90, "timezone": _TZ}
    assert j["pipeline_jobs"].trigger.kwargs == {"minutes": 2, "timezone": _TZ}


def test_trend_scan_interval_tracks_config(monkeypatch):
    _install_fake_apscheduler(monkeypatch)
    _patch_settings(monkeypatch, brands=("hotcars",), enabled=True, interval=15)
    sched = build_scheduler()
    assert sched.jobs["trend_scan:portfolio"].trigger.kwargs["minutes"] == 15


# ---------------------------------------------------------------------------
# run_scheduler — one tick, start + graceful shutdown
# ---------------------------------------------------------------------------


async def test_run_scheduler_starts_prints_and_shuts_down(monkeypatch, capsys):
    _patch_settings(monkeypatch, brands=("hotcars",), enabled=True)

    fake = FakeScheduler(timezone=_TZ)
    fake.add_job(sched_mod._sweep, FakeCron(minute=15), id="ttl_sweep")
    monkeypatch.setattr(sched_mod, "build_scheduler", lambda: fake)

    import asyncio as _asyncio

    async def _cancel(_delay):
        raise _asyncio.CancelledError()

    monkeypatch.setattr(
        sched_mod, "asyncio",
        SimpleNamespace(sleep=_cancel, CancelledError=_asyncio.CancelledError),
    )

    rc = await run_scheduler()

    assert rc == 0
    assert fake.started is True
    assert fake.shutdown_wait is False  # shutdown(wait=False) on the stop path

    out = capsys.readouterr().out
    assert "scheduler running" in out
    assert "ttl_sweep" in out  # each registered job id is printed
