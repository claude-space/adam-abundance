"""Unit tests for the orchestrator glue: cycle, slack, planner.

Scope note: ``plans.py`` (PlanRepo/approval gate) and ``dispatch.py`` are covered
against a real DB in ``tests/integration/test_pipeline.py`` — this file does NOT
duplicate that. Here everything is mocked so it runs with no DB and no network:

* ``cycle`` — ``RunContext.open`` is a fake async CM; ``run_all_observe`` /
  ``Planner`` / ``post_brief`` are stubbed; we assert the observe→synthesize→brief
  ordering, the plan-date default, and the observe-warning branch.
* ``slack`` — a fake ``creds`` drives the flag/token/channel gating; ``httpx`` is
  mocked at the ``AsyncClient`` boundary (never posts to a real webhook);
  ``notify_trend_event`` formatting is asserted via a patched ``post_message``.
* ``planner`` — a fake ``store`` returns hand-built entries per query, a fake repo
  captures ``create_plan``/``add_item``, and ``LLMClient`` is patched to force each
  brief branch (success / empty / unavailable / error).
"""

from __future__ import annotations

import datetime as _dt
import json as _json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

import switchboard.orchestrator.cycle as cycle_mod
import switchboard.orchestrator.planner as planner_mod
import switchboard.orchestrator.slack as slack_mod
from switchboard.adapters.base import AdapterUnavailable
from switchboard.db.enums import EntryType
from switchboard.orchestrator.cycle import run_morning_cycle
from switchboard.orchestrator.planner import ACTION_SPECS, Planner, _Candidate, _SEVERITY_SCORE
from switchboard.orchestrator.slack import notify_trend_event, post_brief, post_message


# ===========================================================================
# cycle.run_morning_cycle
# ===========================================================================


def _cycle_ctx():
    return SimpleNamespace(store=SimpleNamespace())


async def _ok_observe(brand):
    return {"research": "ok"}


async def _noop_post_brief(ctx, brand, text):
    return False


async def test_morning_cycle_order_ctx_and_return(monkeypatch, capsys):
    order: list = []
    ctx = _cycle_ctx()

    async def fake_expire():
        order.append("expire")
        return 0

    ctx.store.expire_stale = fake_expire

    @asynccontextmanager
    async def fake_open(*a, **k):
        yield ctx

    monkeypatch.setattr(cycle_mod.RunContext, "open", fake_open)

    async def fake_observe(brand):
        order.append(("observe", brand))
        return {"research": "ok", "analytics": "ok"}

    monkeypatch.setattr(cycle_mod, "run_all_observe", fake_observe)

    captured: dict = {}

    class FakePlanner:
        def __init__(self, c):
            captured["planner_ctx"] = c

        async def plan(self, brand, plan_date):
            order.append(("plan", brand, plan_date))
            captured["plan_args"] = (brand, plan_date)
            return 42, "THE BRIEF"

    monkeypatch.setattr(cycle_mod, "Planner", FakePlanner)

    async def fake_post_brief(c, brand, text):
        order.append(("brief", brand, text))
        captured["brief_args"] = (c, brand, text)
        return True

    monkeypatch.setattr(cycle_mod, "post_brief", fake_post_brief)

    d = _dt.date(2026, 7, 21)
    rc = await run_morning_cycle("hotcars", d)

    assert rc == 0
    # expire_stale (1st txn) -> observe -> plan -> brief (2nd txn), strictly ordered.
    assert order == [
        "expire", ("observe", "hotcars"), ("plan", "hotcars", d), ("brief", "hotcars", "THE BRIEF"),
    ]
    assert captured["plan_args"] == ("hotcars", d)
    assert captured["planner_ctx"] is ctx
    assert captured["brief_args"][0] is ctx
    assert captured["brief_args"][1:] == ("hotcars", "THE BRIEF")

    out = capsys.readouterr().out
    assert "Draft plan #42 created for hotcars" in out
    assert "THE BRIEF" in out


async def test_morning_cycle_defaults_plan_date_to_today(monkeypatch):
    ctx = _cycle_ctx()
    ctx.store.expire_stale = _ok_expire

    @asynccontextmanager
    async def fake_open(*a, **k):
        yield ctx

    monkeypatch.setattr(cycle_mod.RunContext, "open", fake_open)
    monkeypatch.setattr(cycle_mod, "run_all_observe", _ok_observe)

    got: dict = {}

    class FakePlanner:
        def __init__(self, c):
            pass

        async def plan(self, brand, plan_date):
            got["date"] = plan_date
            return 1, "b"

    monkeypatch.setattr(cycle_mod, "Planner", FakePlanner)
    monkeypatch.setattr(cycle_mod, "post_brief", _noop_post_brief)

    await run_morning_cycle("hotcars")
    assert got["date"] == _dt.date.today()


async def test_morning_cycle_reports_observe_warnings(monkeypatch, capsys):
    ctx = _cycle_ctx()
    ctx.store.expire_stale = _ok_expire

    @asynccontextmanager
    async def fake_open(*a, **k):
        yield ctx

    monkeypatch.setattr(cycle_mod.RunContext, "open", fake_open)

    async def fake_observe(brand):
        return {"research": "ok", "analytics": "error: boom", "reporting": "error: x"}

    monkeypatch.setattr(cycle_mod, "run_all_observe", fake_observe)

    class FakePlanner:
        def __init__(self, c):
            pass

        async def plan(self, brand, plan_date):
            return 7, "brief"

    monkeypatch.setattr(cycle_mod, "Planner", FakePlanner)
    monkeypatch.setattr(cycle_mod, "post_brief", _noop_post_brief)

    rc = await run_morning_cycle("hotcars")
    assert rc == 0
    out = capsys.readouterr().out
    assert "observe warnings" in out
    assert "analytics" in out and "reporting" in out


async def _ok_expire():
    return 0


# ===========================================================================
# slack
# ===========================================================================


class SlackCreds:
    def __init__(self, values: dict):
        self._d = values

    def resolve(self, key, *, required: bool = False, secret: bool = True):
        return self._d.get(key)

    def slack_bot_token(self, brand=None):
        if brand:
            tok = self._d.get(f"SLACK_BOT_TOKEN_{brand.upper()}")
            if tok:
                return tok
        return self._d.get("SLACK_BOT_TOKEN")


def _slack_ctx(values: dict):
    return SimpleNamespace(creds=SlackCreds(values))


def _install_mock_httpx(monkeypatch, responder):
    real = httpx.AsyncClient
    log: dict = {"requests": [], "ctor": []}

    def handler(request):
        log["requests"].append(request)
        return responder(request)

    def factory(*args, **kwargs):
        log["ctor"].append(dict(kwargs))
        kwargs = dict(kwargs)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return log


# -- send gating -------------------------------------------------------------


async def test_post_message_disabled_by_default_logs_only(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("httpx must not be used while notify is disabled")

    monkeypatch.setattr(httpx, "AsyncClient", boom)
    ctx = _slack_ctx({"SLACK_BOT_TOKEN": "t", "SLACK_CHANNEL_ID": "C1"})
    assert await post_message(ctx, "hotcars", "hi") is False


async def test_post_message_enabled_but_no_token(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no post without a token")

    monkeypatch.setattr(httpx, "AsyncClient", boom)
    ctx = _slack_ctx({"SLACK_NOTIFY_ENABLED": "1", "SLACK_CHANNEL_ID": "C1"})
    assert await post_message(ctx, "hotcars", "hi") is False


async def test_post_message_enabled_but_no_channel(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no post without a channel")

    monkeypatch.setattr(httpx, "AsyncClient", boom)
    ctx = _slack_ctx({"SLACK_NOTIFY_ENABLED": "true", "SLACK_BOT_TOKEN": "t"})
    assert await post_message(ctx, "hotcars", "hi") is False


# -- actual send -------------------------------------------------------------


async def test_post_message_posts_when_fully_configured(monkeypatch):
    log = _install_mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"ok": True}))
    ctx = _slack_ctx({"SLACK_NOTIFY_ENABLED": "yes", "SLACK_BOT_TOKEN": "tok-123",
                      "SLACK_CHANNEL_ID": "C-GEN"})
    ok = await post_message(ctx, "hotcars", "hello world")
    assert ok is True

    (req,) = log["requests"]
    assert req.method == "POST"
    assert str(req.url) == "https://slack.com/api/chat.postMessage"
    assert req.headers["Authorization"] == "Bearer tok-123"
    assert _json.loads(req.content) == {"channel": "C-GEN", "text": "hello world", "mrkdwn": True}
    assert log["ctor"][0]["timeout"] == 15.0


async def test_post_message_returns_false_when_slack_not_ok(monkeypatch):
    _install_mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"ok": False, "error": "x"}))
    ctx = _slack_ctx({"SLACK_NOTIFY_ENABLED": "1", "SLACK_BOT_TOKEN": "t", "SLACK_CHANNEL_ID": "C"})
    assert await post_message(ctx, "hotcars", "x") is False


async def test_channel_env_override_wins(monkeypatch):
    log = _install_mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"ok": True}))
    ctx = _slack_ctx({"SLACK_NOTIFY_ENABLED": "1", "SLACK_BOT_TOKEN": "t",
                      "SLACK_CHANNEL_ID_TRENDS": "C-TRENDS",
                      "SLACK_CHANNEL_ID_HOTCARS": "C-HC", "SLACK_CHANNEL_ID": "C-GEN"})
    await post_message(ctx, "hotcars", "x", channel_env="SLACK_CHANNEL_ID_TRENDS")
    assert _json.loads(log["requests"][0].content)["channel"] == "C-TRENDS"


async def test_channel_brand_specific_then_generic(monkeypatch):
    # One shared mock transport; two posts. Brand-specific channel wins for
    # hotcars; carbuzz (no brand-specific channel) falls back to the generic one.
    log = _install_mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"ok": True}))
    ctx = _slack_ctx({"SLACK_NOTIFY_ENABLED": "1", "SLACK_BOT_TOKEN": "t",
                      "SLACK_CHANNEL_ID_HOTCARS": "C-HC", "SLACK_CHANNEL_ID": "C-GEN"})
    await post_message(ctx, "hotcars", "x")
    ctx2 = _slack_ctx({"SLACK_NOTIFY_ENABLED": "1", "SLACK_BOT_TOKEN": "t", "SLACK_CHANNEL_ID": "C-GEN"})
    await post_message(ctx2, "carbuzz", "x")
    channels = [_json.loads(r.content)["channel"] for r in log["requests"]]
    assert channels == ["C-HC", "C-GEN"]


async def test_brand_specific_bot_token_preferred(monkeypatch):
    log = _install_mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"ok": True}))
    ctx = _slack_ctx({"SLACK_NOTIFY_ENABLED": "1", "SLACK_BOT_TOKEN_HOTCARS": "brandtok",
                      "SLACK_BOT_TOKEN": "gentok", "SLACK_CHANNEL_ID": "C"})
    await post_message(ctx, "hotcars", "x")
    assert log["requests"][0].headers["Authorization"] == "Bearer brandtok"


async def test_post_message_transport_error_returns_false(monkeypatch):
    def responder(r):
        raise httpx.ConnectError("down", request=r)

    _install_mock_httpx(monkeypatch, responder)
    ctx = _slack_ctx({"SLACK_NOTIFY_ENABLED": "1", "SLACK_BOT_TOKEN": "t", "SLACK_CHANNEL_ID": "C"})
    assert await post_message(ctx, "hotcars", "x") is False


async def test_post_message_bad_json_returns_false(monkeypatch):
    _install_mock_httpx(monkeypatch, lambda r: httpx.Response(200, text="not-json"))
    ctx = _slack_ctx({"SLACK_NOTIFY_ENABLED": "1", "SLACK_BOT_TOKEN": "t", "SLACK_CHANNEL_ID": "C"})
    assert await post_message(ctx, "hotcars", "x") is False


async def test_post_brief_delegates_with_brief_label(monkeypatch):
    seen: dict = {}

    async def fake_pm(ctx, brand, text, *, channel_env=None, what="message"):
        seen.update(brand=brand, text=text, channel_env=channel_env, what=what)
        return True

    monkeypatch.setattr(slack_mod, "post_message", fake_pm)
    ok = await post_brief(_slack_ctx({}), "hotcars", "the brief")
    assert ok is True
    assert seen == {"brand": "hotcars", "text": "the brief", "channel_env": None, "what": "brief"}


# -- notify_trend_event formatting (post_message patched) --------------------


def _capture_post_message(monkeypatch):
    cap: dict = {}

    async def fake_pm(ctx, brand, text, *, channel_env=None, what="message"):
        cap.update(ctx=ctx, brand=brand, text=text, channel_env=channel_env, what=what)
        return True

    monkeypatch.setattr(slack_mod, "post_message", fake_pm)
    return cap


async def test_notify_trend_event_known_event_full(monkeypatch):
    cap = _capture_post_message(monkeypatch)
    ctx = _slack_ctx({"SWITCHBOARD_PUBLIC_URL": "https://sb.example.com/"})
    ok = await notify_trend_event(
        ctx, "hotcars", "trigger_requested", headline="Big EV news",
        trend_id=12, pipeline_id=34, score=87.6, detail="extra detail",
    )
    assert ok is True
    lines = cap["text"].split("\n")
    assert lines[0] == ":rotating_light: *Trend pipeline request* — approve or decline"
    assert lines[1] == "> Big EV news"
    assert lines[2] == "brand: hotcars  ·  score: 88"          # %.0f rounds 87.6
    assert lines[3] == "https://sb.example.com/trends/12"       # base rstrip('/')
    assert lines[4] == "https://sb.example.com/pipelines/34"
    assert lines[5] == "extra detail"
    assert cap["channel_env"] == "SLACK_CHANNEL_ID_TRENDS"
    assert cap["what"] == "trend:trigger_requested"
    assert cap["brand"] == "hotcars"


async def test_notify_trend_event_unknown_event_minimal(monkeypatch):
    cap = _capture_post_message(monkeypatch)
    await notify_trend_event(_slack_ctx({}), "carbuzz", "weird_event", headline="H")
    lines = cap["text"].split("\n")
    assert lines[0] == "*Trend event: weird_event*"
    assert lines[1] == "> H"
    assert lines[2] == "brand: carbuzz"   # no score line
    assert len(lines) == 3                # no trend/pipeline/detail lines
    assert cap["what"] == "trend:weird_event"


async def test_notify_trend_event_console_fallback_links(monkeypatch):
    cap = _capture_post_message(monkeypatch)
    await notify_trend_event(_slack_ctx({}), "hotcars", "previews_ready",
                             headline="H", trend_id=5, pipeline_id=9)
    lines = cap["text"].split("\n")
    assert lines[0] == ":art: *Content previews ready for review*"
    assert "→ console: /trends/5" in lines
    assert "→ console: /pipelines/9" in lines


async def test_notify_trend_event_score_only(monkeypatch):
    cap = _capture_post_message(monkeypatch)
    await notify_trend_event(_slack_ctx({}), "hotcars", "pipeline_approved",
                             headline="H", score=60.0)
    assert cap["text"].split("\n")[2] == "brand: hotcars  ·  score: 60"


# ===========================================================================
# planner
# ===========================================================================


def _entry(payload, *, id=None, source_system=None):
    return SimpleNamespace(payload=payload, id=id, source_system=source_system)


class PlannerStore:
    def __init__(self, *, flags=None, contexts=None, reports=None, drafts=None, metrics=None):
        self.flags = flags or []
        self.contexts = contexts or []
        self.reports = reports or []
        self.drafts = drafts or []
        self.metrics = metrics or []

    async def query(self, *, brand=None, types=None, source_system=None,
                    fresh_within_seconds=None, limit=200, **kw):
        if source_system == "hc_viral_hits":
            return self.metrics
        t = types[0] if types else None
        return {
            EntryType.FLAG: self.flags,
            EntryType.CONTEXT: self.contexts,
            EntryType.REPORT: self.reports,
            EntryType.DISTRIBUTION_DRAFT: self.drafts,
        }.get(t, [])


class FakeRepo:
    def __init__(self):
        self.items: list = []
        self.created = None
        self.plan = SimpleNamespace(id=123)

    async def create_plan(self, brand, plan_date):
        self.created = (brand, plan_date)
        return self.plan

    async def add_item(self, plan, *, rank, assigned_agent, action_type, params,
                       rationale, cost_estimate, dry_run):
        self.items.append(SimpleNamespace(
            rank=rank, assigned_agent=assigned_agent, action_type=action_type,
            params=params, rationale=rationale, cost_estimate=cost_estimate, dry_run=dry_run,
        ))
        return SimpleNamespace(id=len(self.items))


def _planner_ctx(store):
    return SimpleNamespace(
        store=store, session=None,
        settings=SimpleNamespace(models=SimpleNamespace(synthesis="synth-model")),
    )


def _mk_planner(store=None):
    p = Planner(_planner_ctx(store or PlannerStore()))
    p.repo = FakeRepo()
    return p


def _patch_llm(monkeypatch, *, text=None, exc=None):
    class FakeLLM:
        def __init__(self, ctx):
            pass

        async def complete(self, **kw):
            if exc is not None:
                raise exc
            return SimpleNamespace(text=text)

    monkeypatch.setattr(planner_mod, "LLMClient", FakeLLM)


# -- _Candidate agent/cost mapping ------------------------------------------


def test_candidate_agent_and_cost_from_action_specs():
    c = _Candidate(50, "route_to_writer", {}, "r")
    assert c.agent == "production"
    assert c.cost == {"llm_micros": 800_000}

    u = _Candidate(1, "no_such_action", {}, "r")
    assert u.agent == "orchestrator"
    assert u.cost == {}

    # .cost returns a fresh copy — mutating it must not corrupt ACTION_SPECS.
    c.cost["llm_micros"] = 1
    assert ACTION_SPECS["route_to_writer"][1] == {"llm_micros": 800_000}


# -- _from_flags branches ----------------------------------------------------


def test_from_flags_emaki_backlog_with_ready_ids():
    p = _mk_planner()
    metrics = [_entry({"kind": "hc_viral_queue", "ready_topic_ids": [111, 222]})]
    flags = [_entry({"kind": "emaki_backlog", "ready_count": 3, "severity": "high"})]
    (c,) = p._from_flags(flags, metrics)
    assert c.action_type == "emaki_publish_draft"
    assert c.score == _SEVERITY_SCORE["high"] + 20
    assert c.params == {"topic_id": 111, "source": "hc_viral_hits"}
    assert "3 drafts ready" in c.rationale
    assert c.agent == "production"
    assert c.cost == {}


def test_from_flags_emaki_backlog_without_ready_ids_falls_to_else():
    p = _mk_planner()
    flags = [_entry({"kind": "emaki_backlog", "severity": "medium"})]
    (c,) = p._from_flags(flags, [])  # no queue metric -> ready_ids empty
    assert c.action_type == "notify"
    assert c.params["message"] == "emaki_backlog"
    assert c.params["flag"] == {"kind": "emaki_backlog", "severity": "medium"}
    assert c.score == 60


def test_from_flags_overdue_and_stuck_outlines():
    p = _mk_planner()
    (c1,) = p._from_flags([_entry({"kind": "overdue_outlines", "count": 4})], [])
    assert c1.action_type == "notify"
    assert "4 item(s)" in c1.params["message"]

    (c2,) = p._from_flags([_entry({"kind": "stuck_outlines", "pending": 2})], [])
    assert "2 item(s)" in c2.params["message"]  # falls back to 'pending' when no 'count'


def test_from_flags_writer_failures():
    p = _mk_planner()
    (c,) = p._from_flags([_entry({"kind": "writer_failures", "count": 7})], [])
    assert c.action_type == "notify"
    assert "7" in c.params["message"]


def test_from_flags_spend_cap_exceeded_forces_high_score():
    p = _mk_planner()
    (c,) = p._from_flags(
        [_entry({"kind": "spend_cap_exceeded", "metric": "llm_micros",
                 "scope": "per_run", "severity": "low"})], [])
    assert c.action_type == "notify"
    assert c.score == _SEVERITY_SCORE["high"] == 100  # severity ignored -> always high
    assert "llm_micros" in c.params["message"] and "per_run" in c.params["message"]


def test_from_flags_decay_candidate():
    p = _mk_planner()
    (c,) = p._from_flags(
        [_entry({"kind": "decay_candidate", "url": "https://x",
                 "pos_delta": 2.4, "severity": "medium"})], [])
    assert c.action_type == "queue_decay_refresh"
    assert c.score == 60 + 5
    assert c.params == {"url": "https://x", "pos_delta": 2.4}
    assert c.agent == "production"
    assert c.cost == {"ahrefs_units": 100}


def test_from_flags_content_audit_finding():
    p = _mk_planner()
    (c,) = p._from_flags(
        [_entry({"kind": "content_audit_finding", "url": "https://y",
                 "depth_pct": 40, "avd_seconds": 22, "severity": "high"})], [])
    assert c.action_type == "create_asana_task"
    assert c.score == 100
    assert "https://y" in c.params["name"]
    assert "40" in c.params["notes"] and "22" in c.params["notes"]
    assert c.agent == "production"


def test_from_flags_unknown_kind_and_default_kind():
    p = _mk_planner()
    (c,) = p._from_flags([_entry({"kind": "mystery", "severity": "low"})], [])
    assert c.action_type == "notify"
    assert c.params["message"] == "mystery"
    assert c.score == _SEVERITY_SCORE["low"] == 30

    (c2,) = p._from_flags([_entry({})], [])   # no kind -> defaults to "flag"
    assert c2.params["message"] == "flag"
    assert c2.score == 60                      # default severity medium


# -- _from_candidates --------------------------------------------------------


def test_from_candidates_dedup_and_viral_score():
    p = _mk_planner()
    entries = [
        _entry({"kind": "viral_topic_candidate", "topic_id": 1, "title": "A", "source": "hcviral"},
               source_system="hc"),
        _entry({"kind": "topic_candidate", "topic_id": 1, "title": "A dup"}, source_system="albert"),
        _entry({"kind": "topic_candidate", "title": "B"}, source_system="albert"),
        _entry({"kind": "not_a_topic", "title": "ignore"}, source_system="x"),
    ]
    out = p._from_candidates(entries)
    assert len(out) == 2
    assert out[0].score == 55  # viral variant
    assert out[0].action_type == "route_to_writer"
    assert out[0].params["topic_id"] == 1
    assert out[0].params["source"] == "hcviral"
    assert out[1].params["title"] == "B"
    assert out[1].score == 50
    assert out[1].params["source"] == "albert"  # source falls back to c.source_system


def test_from_candidates_caps_at_five():
    p = _mk_planner()
    entries = [_entry({"kind": "topic_candidate", "topic_id": i, "title": f"T{i}"},
                      source_system="s") for i in range(8)]
    assert len(p._from_candidates(entries)) == 5


# -- _from_distribution ------------------------------------------------------


def test_from_distribution_digest_newsletter_social():
    p = _mk_planner()
    reports = [
        _entry({"kind": "daily_digest_inputs", "ready": True}, id=10),
        _entry({"kind": "daily_digest_inputs", "ready": False}, id=11),  # not ready -> skip
        _entry({"kind": "something_else"}, id=12),
    ]
    drafts = [
        _entry({"kind": "newsletter_draft"}, id=20),
        _entry({"kind": "social_draft"}, id=21),
        _entry({"kind": "other"}, id=22),
    ]
    out = p._from_distribution(reports, drafts, "carbuzz")
    by_action = {(c.action_type, c.score) for c in out}
    assert ("assemble_digest", 55) in by_action
    assert ("assemble_newsletter", 45) in by_action
    assert ("assemble_social_post", 40) in by_action
    assert len(out) == 3
    digest = next(c for c in out if c.action_type == "assemble_digest")
    assert digest.params == {"report_entry_id": 10}


# -- _brief branches ---------------------------------------------------------


async def test_brief_uses_llm_text_when_available(monkeypatch):
    _patch_llm(monkeypatch, text="POLISHED")
    p = _mk_planner()
    brief = await p._brief("hotcars", _dt.date(2026, 7, 21),
                           [_Candidate(60, "notify", {"message": "m"}, "why")])
    assert brief == "POLISHED"


async def test_brief_empty_llm_text_falls_back_to_deterministic(monkeypatch):
    _patch_llm(monkeypatch, text="   ")
    p = _mk_planner()
    brief = await p._brief("hotcars", _dt.date(2026, 7, 21),
                           [_Candidate(60, "notify", {"message": "m"}, "why")])
    assert brief.startswith("*Switchboard — hotcars plan for 2026-07-21*")
    assert "`notify` → orchestrator — why" in brief


async def test_brief_adapter_unavailable_falls_back(monkeypatch):
    _patch_llm(monkeypatch, exc=AdapterUnavailable("no key"))
    p = _mk_planner()
    brief = await p._brief("hotcars", _dt.date(2026, 7, 21), [_Candidate(10, "notify", {}, "r")])
    assert brief.startswith("*Switchboard — hotcars plan for 2026-07-21*")


async def test_brief_generic_exception_falls_back(monkeypatch):
    _patch_llm(monkeypatch, exc=ValueError("boom"))
    p = _mk_planner()
    brief = await p._brief("hotcars", _dt.date(2026, 7, 21), [_Candidate(10, "notify", {}, "r")])
    assert "1. `notify` → orchestrator — r" in brief


# -- plan() end-to-end (fake store + repo + LLM) -----------------------------


async def test_plan_ranks_sorts_and_persists(monkeypatch):
    _patch_llm(monkeypatch, exc=AdapterUnavailable("no key"))  # deterministic brief
    store = PlannerStore(
        flags=[_entry({"kind": "decay_candidate", "url": "u", "pos_delta": 3, "severity": "high"})],
        contexts=[_entry({"kind": "topic_candidate", "topic_id": 1, "title": "T"},
                          source_system="albert")],
        reports=[_entry({"kind": "daily_digest_inputs", "ready": True}, id=9)],
    )
    p = _mk_planner(store)
    plan_id, brief = await p.plan("hotcars", _dt.date(2026, 7, 21))

    assert plan_id == 123
    assert p.repo.created == ("hotcars", _dt.date(2026, 7, 21))
    # decay(105) > digest(55) > topic(50)
    assert [(i.rank, i.action_type) for i in p.repo.items] == [
        (1, "queue_decay_refresh"), (2, "assemble_digest"), (3, "route_to_writer"),
    ]
    decay = p.repo.items[0]
    assert decay.assigned_agent == "production"
    assert decay.cost_estimate == {"ahrefs_units": 100}
    assert decay.dry_run is True
    assert brief.startswith("*Switchboard — hotcars plan for 2026-07-21*")


async def test_plan_empty_memory_emits_default_notify(monkeypatch):
    _patch_llm(monkeypatch, exc=AdapterUnavailable("x"))
    p = _mk_planner(PlannerStore())
    plan_id, _brief = await p.plan("hotcars", _dt.date(2026, 7, 21))
    assert plan_id == 123
    assert len(p.repo.items) == 1
    item = p.repo.items[0]
    assert item.action_type == "notify"
    assert item.rank == 1
    assert item.params["message"] == "No significant signals for hotcars today."


async def test_plan_caps_at_twelve_items(monkeypatch):
    _patch_llm(monkeypatch, exc=AdapterUnavailable("x"))
    flags = [_entry({"kind": "writer_failures", "count": i}) for i in range(15)]
    p = _mk_planner(PlannerStore(flags=flags))
    await p.plan("hotcars")
    assert len(p.repo.items) == 12
    assert [i.rank for i in p.repo.items] == list(range(1, 13))
