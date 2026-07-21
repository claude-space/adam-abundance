"""CLI surface tests (``switchboard.cli``).

The CLI is a thin argparse dispatcher: every ``cmd_*`` handler lazily imports its
heavy dependency *inside the function body* (``from .context import RunContext``,
``from .orchestrator import run_morning_cycle`` …) and drives it through
``asyncio.run``. That lazy-import pattern is what makes these tests hermetic — we
monkeypatch the attribute on the *source* module and the ``from … import …``
inside the handler picks up the fake. Nothing here touches a real DB, network,
event loop server, or the LLM: every boundary below the CLI is mocked.

Tests are plain ``def`` (sync): each ``main([...])`` call itself spins an event
loop via ``asyncio.run``, so the test must NOT already be inside one.
(``asyncio_mode="auto"`` only matters for the async autouse fixture in conftest.)
"""

from __future__ import annotations

import logging
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock

from switchboard import cli
from switchboard.config import get_settings


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------

def _make_fake_ctx() -> MagicMock:
    """A stand-in RunContext value with the store/session methods the handlers
    touch wired as awaitables."""
    ctx = MagicMock(name="ctx")
    ctx.store.expire_stale = AsyncMock(return_value=3)
    ctx.store.supersede_duplicates = AsyncMock(return_value=2)
    ctx.session.refresh = AsyncMock()
    return ctx


class _FakeOpenCM:
    async def __aenter__(self):
        ctx = _make_fake_ctx()
        _FakeRunContext.opened.append(ctx)
        return ctx

    async def __aexit__(self, *exc):
        return False


class _FakeRunContext:
    """Replaces ``switchboard.context.RunContext``. ``.open()`` mirrors the real
    ``@classmethod @asynccontextmanager`` signature (optional url)."""

    opened: list = []

    @classmethod
    def open(cls, url: str | None = None):
        return _FakeOpenCM()


@pytest.fixture
def run_context(monkeypatch):
    """Patch RunContext everywhere the handlers import it from and expose the
    list of yielded ctx objects (``run_context.opened[-1]`` is the last one)."""
    _FakeRunContext.opened = []
    monkeypatch.setattr("switchboard.context.RunContext", _FakeRunContext)
    return _FakeRunContext


@pytest.fixture(autouse=True)
def _quiet_logging(monkeypatch):
    """Neutralize setup_logging for every test so a real ``main()`` call doesn't
    reconfigure root logging handlers. Tests that assert on the log level patch
    it themselves with their own mock (this one is overridden per-call)."""
    monkeypatch.setattr(cli, "setup_logging", MagicMock())


# ---------------------------------------------------------------------------
# Parser structure: defaults, types, dispatch wiring (no handlers invoked)
# ---------------------------------------------------------------------------

def test_parser_dispatches_each_subcommand_to_its_handler():
    p = cli.build_parser()
    cases = {
        "selfcheck": cli.cmd_selfcheck,
        "sweep": cli.cmd_sweep,
        "schedule": cli.cmd_schedule,
        "version": cli.cmd_version,
    }
    for name, func in cases.items():
        assert p.parse_args([name]).func is func


def test_parser_defaults_and_types():
    p = cli.build_parser()
    # top-level default
    assert p.parse_args(["version"]).log_level == "INFO"
    # trend-scan brand is optional, defaults to portfolio
    assert p.parse_args(["trend-scan"]).brand == "portfolio"
    assert p.parse_args(["trend-scan", "hotcars"]).brand == "hotcars"
    # pipeline-worker --limit defaults to 5 and is coerced to int
    assert p.parse_args(["pipeline-worker"]).limit == 5
    assert p.parse_args(["pipeline-worker", "--limit", "12"]).limit == 12
    # serve defaults
    srv = p.parse_args(["serve"])
    assert srv.port is None and srv.reload is False
    assert p.parse_args(["serve", "--port", "9000", "--reload"]).port == 9000
    assert p.parse_args(["serve", "--reload"]).reload is True
    # dispatch plan_id is coerced to int; observe takes agent + brand positionals
    assert p.parse_args(["dispatch", "7"]).plan_id == 7
    obs = p.parse_args(["observe", "research", "hotcars"])
    assert (obs.agent, obs.brand) == ("research", "hotcars")


# ---------------------------------------------------------------------------
# Argparse-level errors / help (SystemExit + exit codes)
# ---------------------------------------------------------------------------

def test_no_subcommand_errors_exit_2(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 2
    assert "required" in capsys.readouterr().err.lower()


def test_unknown_subcommand_errors_exit_2(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["frobnicate"])
    assert e.value.code == 2
    assert "invalid choice" in capsys.readouterr().err.lower()


def test_top_level_help_exit_0_lists_commands(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "usage" in out.lower()
    for name in ("selfcheck", "sweep", "observe", "cycle", "trend-scan", "serve"):
        assert name in out


def test_subcommand_help_exit_0(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["serve", "--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "--port" in out and "--reload" in out


def test_observe_missing_positionals_exit_2():
    with pytest.raises(SystemExit) as e:
        cli.main(["observe"])
    assert e.value.code == 2


def test_observe_missing_brand_exit_2():
    with pytest.raises(SystemExit) as e:
        cli.main(["observe", "research"])
    assert e.value.code == 2


def test_dispatch_noninteger_plan_id_exit_2():
    with pytest.raises(SystemExit) as e:
        cli.main(["dispatch", "not-an-int"])
    assert e.value.code == 2


def test_pipeline_worker_noninteger_limit_exit_2():
    with pytest.raises(SystemExit) as e:
        cli.main(["pipeline-worker", "--limit", "lots"])
    assert e.value.code == 2


def test_feed_invalid_feeder_choice_exit_2(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["feed", "bogus", "hotcars"])
    assert e.value.code == 2
    assert "invalid choice" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# main() wiring: log level + return-value passthrough
# ---------------------------------------------------------------------------

def test_main_returns_handler_exit_code(monkeypatch):
    monkeypatch.setattr(cli, "cmd_version", lambda args: 0)
    assert cli.main(["version"]) == 0
    monkeypatch.setattr(cli, "cmd_version", lambda args: 7)
    assert cli.main(["version"]) == 7


def test_main_log_level_default_is_info(monkeypatch):
    setup = MagicMock()
    monkeypatch.setattr(cli, "setup_logging", setup)
    cli.main(["version"])
    assert setup.call_args.args[0] == logging.INFO


def test_main_log_level_custom_is_upcased(monkeypatch):
    setup = MagicMock()
    monkeypatch.setattr(cli, "setup_logging", setup)
    cli.main(["--log-level", "debug", "version"])
    assert setup.call_args.args[0] == logging.DEBUG


def test_main_log_level_unknown_falls_back_to_info(monkeypatch):
    setup = MagicMock()
    monkeypatch.setattr(cli, "setup_logging", setup)
    cli.main(["--log-level", "banana", "version"])
    assert setup.call_args.args[0] == logging.INFO


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

def test_version_prints_and_returns_0(capsys):
    from switchboard import __version__

    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"switchboard {__version__}"


# ---------------------------------------------------------------------------
# selfcheck  (DB path + redaction helper mocked; also a real redaction probe)
# ---------------------------------------------------------------------------

def test_check_redaction_real_behavior_passes(capsys):
    # Exercises the genuine redact()/register_secret() path — no mocks.
    assert cli._check_redaction() is True
    out = capsys.readouterr().out
    assert out.count("PASS") == 2 and "FAIL" not in out


def test_selfcheck_all_pass_returns_0(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_check_redaction", MagicMock(return_value=True))
    monkeypatch.setattr(cli, "_check_db", AsyncMock(return_value=True))
    assert cli.main(["selfcheck"]) == 0
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out
    # settings summary lines rendered from real get_settings()
    assert "Config:" in out and "Models:" in out


def test_selfcheck_db_fail_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_check_redaction", MagicMock(return_value=True))
    monkeypatch.setattr(cli, "_check_db", AsyncMock(return_value=False))
    assert cli.main(["selfcheck"]) == 1
    assert "RESULT: FAIL" in capsys.readouterr().out


def test_selfcheck_redaction_fail_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_check_redaction", MagicMock(return_value=False))
    monkeypatch.setattr(cli, "_check_db", AsyncMock(return_value=True))
    assert cli.main(["selfcheck"]) == 1
    assert "RESULT: FAIL" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------

def test_sweep_runs_store_maintenance(run_context, capsys):
    assert cli.main(["sweep"]) == 0
    ctx = run_context.opened[-1]
    ctx.store.expire_stale.assert_awaited_once_with()
    ctx.store.supersede_duplicates.assert_awaited_once_with()
    assert "Swept 3 stale entries; superseded 2 duplicate snapshots." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# observe  (happy path + Phase-2-unavailable fallback)
# ---------------------------------------------------------------------------

def test_observe_builds_agent_and_runs_pass(run_context, monkeypatch, capsys):
    agent = MagicMock()
    agent.observe = AsyncMock()
    build_agent = MagicMock(return_value=agent)
    monkeypatch.setattr("switchboard.agents.build_agent", build_agent)

    assert cli.main(["observe", "research", "hotcars"]) == 0
    ctx = run_context.opened[-1]
    build_agent.assert_called_once_with("research", ctx)
    agent.observe.assert_awaited_once_with("hotcars")
    assert "research.observe(hotcars) complete." in capsys.readouterr().out


def test_observe_when_agents_module_unavailable_returns_1(monkeypatch, capsys):
    # Make `from .agents import build_agent` raise ImportError.
    monkeypatch.setitem(sys.modules, "switchboard.agents", None)
    assert cli.main(["observe", "research", "hotcars"]) == 1
    assert "Agents are not available yet (Phase 2+)." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cycle  (happy path + Phase-3-unavailable fallback)
# ---------------------------------------------------------------------------

def test_cycle_runs_morning_cycle(monkeypatch):
    run_cycle = AsyncMock(return_value=0)
    monkeypatch.setattr("switchboard.orchestrator.run_morning_cycle", run_cycle)
    assert cli.main(["cycle", "hotcars"]) == 0
    run_cycle.assert_awaited_once_with("hotcars")


def test_cycle_passes_through_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        "switchboard.orchestrator.run_morning_cycle", AsyncMock(return_value=3)
    )
    assert cli.main(["cycle", "hotcars"]) == 3


def test_cycle_when_orchestrator_unavailable_returns_1(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "switchboard.orchestrator", None)
    assert cli.main(["cycle", "hotcars"]) == 1
    assert "Orchestrator is not available yet (Phase 3+)." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------

def test_seed_invokes_seed_brand(monkeypatch, capsys):
    seed_brand = AsyncMock(return_value=5)
    monkeypatch.setattr("switchboard.devseed.seed_brand", seed_brand)
    assert cli.main(["seed", "hotcars"]) == 0
    seed_brand.assert_awaited_once_with("hotcars")
    assert "Seeded 5 synthetic memory entries for hotcars (dev/demo only)." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def test_plan_synthesizes_draft(run_context, monkeypatch, capsys):
    planner_instance = MagicMock()
    planner_instance.plan = AsyncMock(return_value=(42, "BRIEF BODY TEXT"))
    Planner = MagicMock(return_value=planner_instance)
    monkeypatch.setattr("switchboard.orchestrator.planner.Planner", Planner)

    assert cli.main(["plan", "hotcars"]) == 0
    ctx = run_context.opened[-1]
    Planner.assert_called_once_with(ctx)
    planner_instance.plan.assert_awaited_once_with("hotcars")
    out = capsys.readouterr().out
    assert "Draft plan #42 for hotcars" in out
    assert "/plans/42" in out
    assert "BRIEF BODY TEXT" in out


# ---------------------------------------------------------------------------
# feed  (valid choices only; invalid choice covered under argparse errors)
# ---------------------------------------------------------------------------

def test_feed_runs_named_feeder(monkeypatch, capsys):
    run_feeder = AsyncMock(return_value=4)
    monkeypatch.setattr("switchboard.feeders.run_feeder", run_feeder)
    assert cli.main(["feed", "decay", "hotcars"]) == 0
    run_feeder.assert_awaited_once_with("decay", "hotcars")
    assert "decay feeder wrote 4 entries for hotcars." in capsys.readouterr().out


def test_feed_accepts_each_valid_choice(monkeypatch):
    run_feeder = AsyncMock(return_value=1)
    monkeypatch.setattr("switchboard.feeders.run_feeder", run_feeder)
    for feeder in ("decay", "content_audit", "trend_scan"):
        assert cli.main(["feed", feeder, "carbuzz"]) == 0
    assert run_feeder.await_count == 3


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------

def test_schedule_runs_scheduler_loop(monkeypatch):
    run_scheduler = AsyncMock(return_value=0)
    monkeypatch.setattr("switchboard.scheduler.run_scheduler", run_scheduler)
    assert cli.main(["schedule"]) == 0
    run_scheduler.assert_awaited_once_with()


# ---------------------------------------------------------------------------
# trend-scan  (scope gate + disabled / error / success / proposed branches)
# ---------------------------------------------------------------------------

def test_trend_scan_rejects_unknown_brand(monkeypatch, capsys):
    scan = AsyncMock()
    monkeypatch.setattr("switchboard.trends.scout.run_trend_scan", scan)
    assert cli.main(["trend-scan", "not-a-brand"]) == 1
    scan.assert_not_awaited()
    assert "Unknown brand" in capsys.readouterr().out


def test_trend_scan_default_scope_is_portfolio(monkeypatch):
    scan = AsyncMock(return_value={"enabled": False})
    monkeypatch.setattr("switchboard.trends.scout.run_trend_scan", scan)
    assert cli.main(["trend-scan"]) == 0
    scan.assert_awaited_once_with("portfolio")


def test_trend_scan_disabled_returns_0(monkeypatch, capsys):
    monkeypatch.setattr(
        "switchboard.trends.scout.run_trend_scan",
        AsyncMock(return_value={"enabled": False}),
    )
    assert cli.main(["trend-scan", "hotcars"]) == 0
    assert "Trend pipeline is disabled" in capsys.readouterr().out


def test_trend_scan_error_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(
        "switchboard.trends.scout.run_trend_scan",
        AsyncMock(return_value={"error": "kill switch engaged"}),
    )
    assert cli.main(["trend-scan", "hotcars"]) == 1
    assert "Scan refused: kill switch engaged" in capsys.readouterr().out


def _scan_summary(**over):
    base = {
        "signals": 10,
        "clusters": 3,
        "new_trends": 2,
        "updated_trends": 1,
        "proposed": 0,
        "suppressed": 4,
        "expired": 1,
    }
    base.update(over)
    return base


def test_trend_scan_success_no_proposed(monkeypatch, capsys):
    monkeypatch.setattr(
        "switchboard.trends.scout.run_trend_scan",
        AsyncMock(return_value=_scan_summary(proposed=0)),
    )
    assert cli.main(["trend-scan", "hotcars"]) == 0
    out = capsys.readouterr().out
    assert "Trend scan (hotcars): 10 signals -> 3 clusters" in out
    assert "new=2 updated=1 proposed=0 suppressed=4 expired=1" in out
    assert "Review pending" not in out


def test_trend_scan_success_with_proposed_prompts_review(monkeypatch, capsys):
    monkeypatch.setattr(
        "switchboard.trends.scout.run_trend_scan",
        AsyncMock(return_value=_scan_summary(proposed=2)),
    )
    assert cli.main(["trend-scan", "hotcars"]) == 0
    out = capsys.readouterr().out
    assert "proposed=2" in out
    assert "Review pending trigger requests at /trends" in out


# ---------------------------------------------------------------------------
# pipeline-worker
# ---------------------------------------------------------------------------

def test_pipeline_worker_default_limit(monkeypatch, capsys):
    sweep = AsyncMock(return_value={"ok": 1, "pending": 2, "failed": 0})
    monkeypatch.setattr("switchboard.trends.pipeline.run_job_sweep", sweep)
    assert cli.main(["pipeline-worker"]) == 0
    sweep.assert_awaited_once_with(limit=5)
    assert "Job sweep: ok=1 pending=2 failed=0" in capsys.readouterr().out


def test_pipeline_worker_custom_limit(monkeypatch):
    sweep = AsyncMock(return_value={"ok": 0, "pending": 0, "failed": 0})
    monkeypatch.setattr("switchboard.trends.pipeline.run_job_sweep", sweep)
    assert cli.main(["pipeline-worker", "--limit", "20"]) == 0
    sweep.assert_awaited_once_with(limit=20)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def test_dispatch_summarizes_plan(run_context, monkeypatch, capsys):
    summary = {
        "dispatched": 2,
        "done": 1,
        "failed": 1,
        "refused": 0,
        "items": [
            {"id": 10, "action": "publish", "result": "ok", "dry_run": True, "summary": "drafted"},
            {"id": 11, "action": "push", "result": "error", "dry_run": False, "reason": "boom"},
        ],
    }
    dispatcher_instance = MagicMock()
    dispatcher_instance.dispatch_plan = AsyncMock(return_value=summary)
    Dispatcher = MagicMock(return_value=dispatcher_instance)
    monkeypatch.setattr("switchboard.orchestrator.dispatch.Dispatcher", Dispatcher)

    assert cli.main(["dispatch", "7"]) == 0
    ctx = run_context.opened[-1]
    Dispatcher.assert_called_once_with(ctx)
    dispatcher_instance.dispatch_plan.assert_awaited_once_with(7)
    out = capsys.readouterr().out
    assert "Dispatch summary for plan 7:" in out
    assert "dispatched: 2" in out and "failed: 1" in out
    assert "item 10 publish: ok" in out and "(dry-run)" in out and "drafted" in out
    assert "item 11 push: error" in out and "(LIVE)" in out and "boom" in out


# ---------------------------------------------------------------------------
# serve  (uvicorn boundary mocked; missing-uvicorn fallback)
# ---------------------------------------------------------------------------

def test_serve_launches_uvicorn_with_explicit_port(monkeypatch):
    import uvicorn

    run_mock = MagicMock()
    monkeypatch.setattr(uvicorn, "run", run_mock)
    settings = get_settings()

    assert cli.main(["serve", "--port", "1234", "--reload"]) == 0
    run_mock.assert_called_once()
    call = run_mock.call_args
    assert call.args[0] == "switchboard.api.app:app"
    assert call.kwargs["host"] == "0.0.0.0"
    assert call.kwargs["port"] == 1234
    assert call.kwargs["reload"] is True
    assert call.kwargs["root_path"] == (settings.base_path or "")


def test_serve_defaults_port_to_settings(monkeypatch):
    import uvicorn

    run_mock = MagicMock()
    monkeypatch.setattr(uvicorn, "run", run_mock)
    settings = get_settings()

    assert cli.main(["serve"]) == 0
    call = run_mock.call_args
    assert call.kwargs["port"] == settings.port
    assert call.kwargs["reload"] is False


def test_serve_without_uvicorn_returns_1(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    assert cli.main(["serve"]) == 1
    assert "uvicorn not installed." in capsys.readouterr().out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
