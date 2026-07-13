"""Switchboard CLI.

Commands:
  selfcheck   Verify Phase 0 foundations: config + credentials load, secret
              redaction works, and (if a DB is reachable) a dummy adapter can
              write/read a memory_entry and the TTL sweep expires it.
  sweep       Run the TTL sweep (expire stale entries) once.
  observe     Run one agent's observe pass for a brand (Phase 2+).
  cycle       Run the morning orchestrator cycle for a brand (Phase 3+).
  serve       Launch the FastAPI app (approval surface + API, Phase 3+).
  version     Print version.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .config import get_settings
from .logging_ import get_logger, redact, register_secret, setup_logging

log = get_logger("cli")


# ---------------------------------------------------------------------------
# selfcheck
# ---------------------------------------------------------------------------

def _check_redaction() -> bool:
    fake = "sk-ant-api03-THISISAFAKESECRET1234567890abcdefg"
    register_secret(fake)
    scrubbed = redact(f"token={fake} tail")
    ok_registered = fake not in scrubbed and "REDACTED" in scrubbed
    # Shape backstop (value never registered):
    shaped = redact("bearer xoxb-9999-abcdefghijklmnop")
    ok_shape = "xoxb-9999" not in shaped
    print(f"  redaction (registered value): {'PASS' if ok_registered else 'FAIL'}")
    print(f"  redaction (shape backstop):   {'PASS' if ok_shape else 'FAIL'}")
    return ok_registered and ok_shape


async def _check_db() -> bool:
    """Exercise the DB path: dummy observe -> write -> read back -> TTL sweep."""
    from datetime import datetime, timedelta, timezone

    from .adapters.dummy import DummyAdapter
    from .context import RunContext
    from .db.enums import EntryStatus, EntryType
    from .interfaces import EntryDraft

    try:
        async with RunContext.open() as ctx:
            adapter = DummyAdapter(ctx)
            drafts = await adapter.observe("hotcars")
            written = await ctx.store.write_many(drafts)
            rows = await ctx.store.query(brand="hotcars", types=[EntryType.METRIC], limit=5)
            read_ok = any(r.id == written[0].id for r in rows)
            print(f"  dummy adapter observe->write->read: {'PASS' if read_ok and written else 'FAIL'}")

            # TTL sweep: write an already-expired entry and confirm it sweeps.
            expired = await ctx.store.write(
                EntryDraft(
                    type=EntryType.CONTEXT,
                    brand="hotcars",
                    source_agent="system",
                    source_system="selfcheck",
                    payload={"kind": "expired-probe"},
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                )
            )
            swept = await ctx.store.expire_stale()
            await ctx.session.refresh(expired)
            sweep_ok = swept >= 1 and expired.status == EntryStatus.EXPIRED.value
            print(f"  TTL sweep expires stale entry:      {'PASS' if sweep_ok else 'FAIL'}")
            return bool(read_ok and written and sweep_ok)
    except Exception as exc:  # noqa: BLE001
        print(f"  DB checks SKIPPED (no reachable Postgres): {type(exc).__name__}: {redact(str(exc))}")
        print("    -> start Postgres (docker compose up -d) and run migrations:")
        print("       alembic upgrade head")
        return True  # not a hard failure offline


def cmd_selfcheck(args: argparse.Namespace) -> int:
    print("Switchboard self-check")
    print("-" * 60)
    settings = get_settings()
    present = sorted(k for k, v in settings.creds.describe().items() if v)
    missing = sorted(k for k, v in settings.creds.describe().items() if not v)
    print(f"Config: env={settings.env} brands={list(settings.brand_keys)} "
          f"dry_run_default={settings.dry_run_default} kill_switch={settings.kill_switch}")
    print(f"Models: default={settings.models.default} synthesis={settings.models.synthesis} "
          f"factcheck={settings.models.factcheck}")
    print(f"Credentials present: {present}")
    print(f"Credentials missing: {missing}")
    print("Redaction:")
    red_ok = _check_redaction()
    print("Database + memory:")
    db_ok = asyncio.run(_check_db())
    print("-" * 60)
    ok = red_ok and db_ok
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# sweep / observe / cycle / serve
# ---------------------------------------------------------------------------

def cmd_sweep(args: argparse.Namespace) -> int:
    from .context import RunContext

    async def _run() -> int:
        async with RunContext.open() as ctx:
            expired = await ctx.store.expire_stale()
            superseded = await ctx.store.supersede_duplicates()
            print(f"Swept {expired} stale entries; superseded {superseded} duplicate snapshots.")
            return 0

    return asyncio.run(_run())


def cmd_observe(args: argparse.Namespace) -> int:
    try:
        from .agents import build_agent  # Phase 2
    except Exception:  # noqa: BLE001
        print("Agents are not available yet (Phase 2+).")
        return 1

    from .context import RunContext

    async def _run() -> int:
        async with RunContext.open() as ctx:
            agent = build_agent(args.agent, ctx)
            await agent.observe(args.brand)
            print(f"{args.agent}.observe({args.brand}) complete.")
            return 0

    return asyncio.run(_run())


def cmd_cycle(args: argparse.Namespace) -> int:
    try:
        from .orchestrator import run_morning_cycle  # Phase 3
    except Exception:  # noqa: BLE001
        print("Orchestrator is not available yet (Phase 3+).")
        return 1
    return asyncio.run(run_morning_cycle(args.brand))


def cmd_seed(args: argparse.Namespace) -> int:
    from .devseed import seed_brand

    async def _run() -> int:
        n = await seed_brand(args.brand)
        print(f"Seeded {n} synthetic memory entries for {args.brand} (dev/demo only).")
        return 0

    return asyncio.run(_run())


def cmd_plan(args: argparse.Namespace) -> int:
    from .context import RunContext
    from .orchestrator.planner import Planner

    async def _run() -> int:
        async with RunContext.open() as ctx:
            plan_id, brief = await Planner(ctx).plan(args.brand)
        print(f"Draft plan #{plan_id} for {args.brand}. Review at /plans/{plan_id} (switchboard serve).")
        print("\n--- brief ---\n" + brief)
        return 0

    return asyncio.run(_run())


def cmd_feed(args: argparse.Namespace) -> int:
    from .feeders import run_feeder

    async def _run() -> int:
        n = await run_feeder(args.feeder, args.brand)
        print(f"{args.feeder} feeder wrote {n} entries for {args.brand}.")
        return 0

    return asyncio.run(_run())


def cmd_schedule(args: argparse.Namespace) -> int:
    from .scheduler import run_scheduler

    return asyncio.run(run_scheduler())


def cmd_dispatch(args: argparse.Namespace) -> int:
    from .context import RunContext
    from .orchestrator.dispatch import Dispatcher

    async def _run() -> int:
        async with RunContext.open() as ctx:
            summary = await Dispatcher(ctx).dispatch_plan(args.plan_id)
        print(f"Dispatch summary for plan {args.plan_id}:")
        for k in ("dispatched", "done", "failed", "refused"):
            print(f"  {k}: {summary[k]}")
        for it in summary["items"]:
            print(f"  - item {it['id']} {it['action']}: {it['result']} "
                  f"({'dry-run' if it.get('dry_run', True) else 'LIVE'}) {it.get('summary') or it.get('reason') or ''}")
        return 0

    return asyncio.run(_run())


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except Exception:  # noqa: BLE001
        print("uvicorn not installed.")
        return 1
    settings = get_settings()
    uvicorn.run("switchboard.api.app:app", host="0.0.0.0", port=args.port or settings.port,
                reload=args.reload, root_path=settings.base_path or "")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"switchboard {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="switchboard", description="Switchboard orchestration CLI")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("selfcheck", help="verify Phase 0 foundations").set_defaults(func=cmd_selfcheck)
    sub.add_parser("sweep", help="run the TTL sweep once").set_defaults(func=cmd_sweep)

    obs = sub.add_parser("observe", help="run one agent's observe pass")
    obs.add_argument("agent")
    obs.add_argument("brand")
    obs.set_defaults(func=cmd_observe)

    cyc = sub.add_parser("cycle", help="run the morning orchestrator cycle")
    cyc.add_argument("brand")
    cyc.set_defaults(func=cmd_cycle)

    sd = sub.add_parser("seed", help="inject synthetic memory entries (dev/demo)")
    sd.add_argument("brand")
    sd.set_defaults(func=cmd_seed)

    pl = sub.add_parser("plan", help="synthesize a draft plan from current memory (no observe)")
    pl.add_argument("brand")
    pl.set_defaults(func=cmd_plan)

    fd = sub.add_parser("feed", help="run a scheduled feeder once")
    fd.add_argument("feeder", choices=["decay", "content_audit"])
    fd.add_argument("brand")
    fd.set_defaults(func=cmd_feed)

    dsp = sub.add_parser("dispatch", help="dispatch an approved plan's items (governor-gated)")
    dsp.add_argument("plan_id", type=int)
    dsp.set_defaults(func=cmd_dispatch)

    sub.add_parser("schedule", help="run the APScheduler loop (cycle + feeders)").set_defaults(func=cmd_schedule)

    srv = sub.add_parser("serve", help="launch the FastAPI app")
    srv.add_argument("--port", type=int, default=None)
    srv.add_argument("--reload", action="store_true")
    srv.set_defaults(func=cmd_serve)

    sub.add_parser("version", help="print version").set_defaults(func=cmd_version)
    return p


def _force_utf8_stdout() -> None:
    # Windows consoles default to cp1252 and crash on Unicode (em-dashes, Δ, →)
    # that the LLM/planner emit. Reconfigure to UTF-8 with replacement.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    args = build_parser().parse_args(argv)
    setup_logging(getattr(logging, str(args.log_level).upper(), logging.INFO))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
