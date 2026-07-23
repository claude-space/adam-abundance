"""Unit tests for the Switchboard agent layer (PRD §6, §14).

The agents orchestrate over a ``RunContext`` — they touch only shared memory
(``ctx.store``), the governor (``ctx.governor``), the DB session (``ctx.session``),
config (``ctx.settings``), and credentials (``ctx.creds``), plus their owned
adapters/clients. Every one of those collaborators is FAKED here so the whole
file runs with **no database and no network**:

* ``FakeStore`` records ``write`` / ``write_many`` / ``supersede`` and answers
  ``query`` from a per-test responder — it never touches Postgres.
* ``FakeGovernor`` records ``charge`` and answers ``within_caps`` from a flag.
* ``FakeSession`` answers ``execute`` from a queued list of ``FakeResult`` and
  assigns autoincrement ids on ``flush`` (so an ORM object's ``.id`` is populated
  the way a real flush would).
* External clients (``BigQueryClient`` / ``LLMClient`` / ``SentinelClient``) are
  monkeypatched at their import module so the analytics/research compute paths run
  against canned data.

``asyncio_mode=auto`` (pyproject) means async tests need no decorator.

Agent construction calls ``build_adapters`` (real adapter *instances* — harmless,
they only stash ctx). Tests that exercise an agent's own logic set
``agent.adapters = []`` (via :func:`make_agent`) so the base observe pass is a
no-op and only the agent-specific code under test runs; tests of ``BaseAgent``'s
adapter loop inject :class:`FakeAdapter` instances instead.
"""

from __future__ import annotations

import json
from collections import deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from switchboard.adapters.base import AdapterUnavailable
from switchboard.adapters.registry import owned_tool_names
from switchboard.agents import (
    AGENT_ORDER,
    AnalyticsAgent,
    OpportunityAgent,
    PaidMediaAgent,
    ProductionAgent,
    ReportingAgent,
    ResearchAgent,
    all_agents,
    build_agent,
    run_all_observe,
)
from switchboard.agents.analytics import refresh_pay_baseline
from switchboard.agents.base import BaseAgent
from switchboard.db.enums import EntryType
from switchboard.db.models import (
    BrandTopicDemand,
    WriterPayBaseline,
    WriterPersona,
    WriterStats,
    WriterStyleProfile,
)
from switchboard.interfaces import ActionResult, EntryDraft, PlanItemView


# ===========================================================================
# Fakes
# ===========================================================================


class FakeGovernor:
    """Records ``charge`` tuples; answers ``within_caps`` from ``within`` (a bool
    or a callable(metric, additional) -> bool)."""

    def __init__(self, within=True) -> None:
        self.charges: list[tuple[str, int, str]] = []
        self.within_calls: list[tuple[str, int]] = []
        self._within = within

    async def charge(self, metric: str, amount: int, agent: str) -> None:
        self.charges.append((metric, int(amount), agent))

    async def within_caps(self, metric: str, *, additional: int = 0) -> bool:
        self.within_calls.append((metric, additional))
        if callable(self._within):
            return bool(self._within(metric, additional))
        return bool(self._within)


class FakeStore:
    """In-memory stand-in for MemoryStore. ``query`` returns [] unless a
    ``query_responder(kwargs) -> list`` is set. Every written draft (single or via
    write_many) lands in ``self.writes``."""

    def __init__(self) -> None:
        self.writes: list[EntryDraft] = []
        self.write_fact_gate: list[bool] = []
        self.write_many_calls: list[tuple[list[EntryDraft], bool]] = []
        self.superseded: list[list[int]] = []
        self.query_log: list[dict] = []
        self.query_responder = None
        self._seq = 0

    async def write(self, draft: EntryDraft, *, fact_gate_ok: bool = False):
        self.writes.append(draft)
        self.write_fact_gate.append(fact_gate_ok)
        self._seq += 1
        return SimpleNamespace(id=self._seq, payload=draft.payload, brand=draft.brand,
                               type=draft.type, verified=draft.verified)

    async def write_many(self, drafts, *, fact_gate_ok: bool = False):
        drafts = list(drafts)
        self.write_many_calls.append((drafts, fact_gate_ok))
        return [await self.write(d, fact_gate_ok=fact_gate_ok) for d in drafts]

    async def supersede(self, entry_ids):
        ids = list(entry_ids)
        self.superseded.append(ids)
        return len(ids)

    async def query(self, **kwargs):
        self.query_log.append(kwargs)
        if self.query_responder is not None:
            return list(self.query_responder(kwargs))
        return []

    async def log_tool_call(self, **kwargs):
        return None


class FakeResult:
    """Stand-in for a SQLAlchemy Result covering the accessors agents use."""

    def __init__(self, *, scalar=None, scalars_list=None, rowcount=0) -> None:
        self._scalar = scalar
        self._scalars = scalars_list if scalars_list is not None else []
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar if self._scalar is not None else 0

    def scalars(self):
        return _FakeScalars(self._scalars)


class _FakeScalars:
    def __init__(self, items) -> None:
        self._items = list(items)

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


class FakeSession:
    """Async session stub. ``execute`` pops the next queued FakeResult (or a blank
    one). ``flush`` populates ids on added ORM objects, mimicking a real flush."""

    def __init__(self, results=None) -> None:
        self.results = deque(results or [])
        self.added: list = []
        self.executed: list = []
        self.flushes = 0
        self._id_seq = 100

    async def execute(self, statement=None, *args, **kwargs):
        self.executed.append(statement)
        if self.results:
            return self.results.popleft()
        return FakeResult()

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1
        for obj in self.added:
            try:
                if getattr(obj, "id", None) is None:
                    self._id_seq += 1
                    obj.id = self._id_seq
            except Exception:
                pass


class FakeCreds:
    """resolve() reads a dict; typed accessors return innocuous values."""

    def __init__(self, values=None) -> None:
        self.values = dict(values or {})

    def resolve(self, key, *, required=False, secret=True):
        return self.values.get(key)

    def google_sa(self):
        return SimpleNamespace(inline_json=None, path=None, project_id="proj")

    def sentinel(self):
        return (self.values.get("SENTINEL_API_KEY"), self.values.get("SENTINEL_ACCOUNT") or "valnet")

    def anthropic_key(self):
        return self.values.get("ANTHROPIC_API_KEY")


class FakeBrand:
    _SHORT = {"hotcars": "HC", "carbuzz": "CB", "topspeed": "TPS"}

    def __init__(self, key: str) -> None:
        self.key = key
        self.short_code = self._SHORT.get(key, key[:3].upper())
        self.discover_name = key.capitalize()
        self.domain = f"{key}.com"

    @property
    def sentinel_property_id(self) -> str:
        return f"www.{self.domain}"


class FakeSettings:
    _BRANDS = ("hotcars", "carbuzz", "topspeed")

    def __init__(self) -> None:
        self.models = SimpleNamespace(default="claude-sonnet-4-6",
                                      synthesis="claude-opus-4-5",
                                      factcheck="claude-haiku-4-5")
        self.dry_run_default = True

    def brand(self, key: str) -> FakeBrand:
        if key not in self._BRANDS:
            raise KeyError(key)
        return FakeBrand(key)

    def is_valid_scope(self, brand: str) -> bool:
        return brand == "portfolio" or brand in self._BRANDS


class FakeAdapter:
    """Read adapter stub for BaseAgent.observe tests: returns canned drafts."""

    def __init__(self, drafts=None) -> None:
        self._drafts = list(drafts or [])
        self.observed: list[str] = []

    async def observe(self, brand: str, **kwargs):
        self.observed.append(brand)
        return list(self._drafts)


# -- fake external clients ---------------------------------------------------


class FakeBQ:
    """BigQueryClient stand-in. Configurable rows/bytes/estimate and optional
    exceptions on estimate/query."""

    def __init__(self, *, rows=None, bytes_processed=0, estimate=0,
                 query_exc=None, estimate_exc=None) -> None:
        self.rows = rows if rows is not None else []
        self.bytes_processed = bytes_processed
        self.estimate = estimate
        self.query_exc = query_exc
        self.estimate_exc = estimate_exc
        self.queries: list = []

    async def estimate_bytes(self, sql, params=None):
        if self.estimate_exc is not None:
            raise self.estimate_exc
        return self.estimate

    async def query(self, sql, params=None):
        self.queries.append((sql, params))
        if self.query_exc is not None:
            raise self.query_exc
        return SimpleNamespace(rows=list(self.rows), bytes_processed=self.bytes_processed, fields=[])


class FakeLLM:
    """LLMClient stand-in for complete()/web_search()."""

    def __init__(self, *, text="", citations=None, complete_exc=None, search_exc=None) -> None:
        self.text = text
        self.citations = list(citations or [])
        self.complete_exc = complete_exc
        self.search_exc = search_exc
        self.complete_calls: list[dict] = []
        self.search_calls: list[dict] = []

    async def complete(self, *, system, prompt, model=None, max_tokens=1024, tools=None, agent="system"):
        self.complete_calls.append({"system": system, "prompt": prompt, "model": model, "agent": agent})
        if self.complete_exc is not None:
            raise self.complete_exc
        return SimpleNamespace(text=self.text, citations=list(self.citations))

    async def web_search(self, query, *, model=None, agent="research", max_tokens=512):
        self.search_calls.append({"query": query, "agent": agent})
        if self.search_exc is not None:
            raise self.search_exc
        return SimpleNamespace(text=self.text, citations=list(self.citations))


# ===========================================================================
# Helpers
# ===========================================================================


def make_ctx(*, creds_values=None, within=True, session=None, store=None):
    return SimpleNamespace(
        store=store if store is not None else FakeStore(),
        governor=FakeGovernor(within=within),
        settings=FakeSettings(),
        creds=FakeCreds(creds_values),
        session=session if session is not None else FakeSession(),
    )


def make_agent(cls, ctx, adapters=None):
    """Build an agent then override its adapters (default: none, so the base
    observe pass is a no-op and only the agent's own logic runs)."""
    agent = cls(ctx)
    agent.adapters = [] if adapters is None else adapters
    return agent


def mem(payload=None, *, id=1, brand="hotcars", type=EntryType.METRIC,
        source_agent="analytics", source_system="bigquery", verified=False):
    return SimpleNamespace(id=id, brand=brand, type=type, source_agent=source_agent,
                           source_system=source_system, payload=payload or {}, verified=verified)


def async_return(value):
    async def _f(*args, **kwargs):
        return value
    return _f


def patch_bq(monkeypatch, fake):
    monkeypatch.setattr("switchboard.adapters.clients.bigquery.BigQueryClient",
                        lambda *a, **k: fake)


def patch_bq_unavailable(monkeypatch, msg="no bq"):
    def boom(*a, **k):
        raise AdapterUnavailable(msg)
    monkeypatch.setattr("switchboard.adapters.clients.bigquery.BigQueryClient", boom)


def patch_analytics_llm(monkeypatch, fake):
    monkeypatch.setattr("switchboard.adapters.clients.llm.LLMClient", lambda ctx: fake)


def patch_research_llm(monkeypatch, fake):
    monkeypatch.setattr("switchboard.agents.research.LLMClient", lambda ctx: fake)


def patch_sentinel(monkeypatch, fake):
    monkeypatch.setattr("switchboard.adapters.clients.sentinel.SentinelClient",
                        lambda *a, **k: fake)


_STYLE_FEATURES_JSON = json.dumps({
    "voice": "wry", "tone": "bold", "sentence_rhythm": "varied", "structure": "news lede",
    "formatting": "subheads", "headline_style": "punchy", "vocabulary": "gearhead",
    "dos": ["lead with the news"], "donts": ["no clickbait"],
})


# ===========================================================================
# BaseAgent
# ===========================================================================


async def test_base_observe_runs_each_adapter_and_sums():
    ctx = make_ctx()
    d1 = EntryDraft(type=EntryType.METRIC, brand="hotcars", source_agent="x", payload={"k": 1})
    d2 = EntryDraft(type=EntryType.FLAG, brand="hotcars", source_agent="x", payload={"k": 2})
    d3 = EntryDraft(type=EntryType.METRIC, brand="hotcars", source_agent="x", payload={"k": 3})
    a1, a2, a3 = FakeAdapter([d1, d2]), FakeAdapter([]), FakeAdapter([d3])
    agent = make_agent(ProductionAgent, ctx, adapters=[a1, a2, a3])

    written = await agent.observe("hotcars")

    assert written == 3  # 2 + 0 (empty adapter, no persist) + 1
    assert a1.observed == a2.observed == a3.observed == ["hotcars"]
    # write_many called only for adapters that returned drafts, always fact_gate False.
    assert [fg for (_, fg) in ctx.store.write_many_calls] == [False, False]
    assert ctx.store.writes == [d1, d2, d3]


async def test_base_observe_empty_adapter_writes_nothing():
    ctx = make_ctx()
    agent = make_agent(ProductionAgent, ctx, adapters=[FakeAdapter([])])
    assert await agent.observe("hotcars") == 0
    assert ctx.store.write_many_calls == []


async def test_base_persist_never_certifies_facts():
    # A base agent persisting a verified draft still goes through fact_gate_ok=False.
    ctx = make_ctx()
    verified_draft = EntryDraft(type=EntryType.FACT, brand="hotcars", source_agent="x",
                                payload={"k": 1}, verified=True)
    agent = make_agent(ProductionAgent, ctx, adapters=[FakeAdapter([verified_draft])])
    await agent.observe("hotcars")
    assert ctx.store.write_many_calls[0][1] is False


def test_owned_tools_matches_registry():
    ctx = make_ctx()
    assert make_agent(AnalyticsAgent, ctx).owned_tools == owned_tool_names("analytics")
    assert make_agent(PaidMediaAgent, ctx).owned_tools == owned_tool_names("paid_media")
    assert make_agent(ReportingAgent, ctx).owned_tools == owned_tool_names("reporting") == []


async def test_execute_no_action_adapter_returns_error(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr("switchboard.agents.base.build_action_adapter", lambda at, c: None)
    agent = make_agent(ProductionAgent, ctx)
    item = PlanItemView(assigned_agent="production", action_type="mystery", dry_run=True)
    res = await agent.execute(item)
    assert res.ok is False
    assert res.action_type == "mystery"
    assert "no action adapter" in res.error
    assert res.dry_run is True


async def test_execute_ownership_violation(monkeypatch):
    ctx = make_ctx()
    other = SimpleNamespace(owner_agent="production")
    monkeypatch.setattr("switchboard.agents.base.build_action_adapter", lambda at, c: other)
    agent = make_agent(ReportingAgent, ctx)  # name == "reporting"
    item = PlanItemView(assigned_agent="reporting", action_type="create_asana_task", dry_run=True)
    res = await agent.execute(item)
    assert res.ok is False
    assert "ownership violation" in res.error
    assert "reporting" in res.error and "production" in res.error
    assert res.action_type == "create_asana_task"


async def test_execute_success_delegates_and_passes_dry_run(monkeypatch):
    ctx = make_ctx()
    captured = {}

    async def fake_act(item, *, dry_run):
        captured["dry_run"] = dry_run
        captured["item"] = item
        return ActionResult(ok=True, dry_run=dry_run, action_type=item.action_type, summary="did it")

    adapter = SimpleNamespace(owner_agent="production", act=fake_act)
    monkeypatch.setattr("switchboard.agents.base.build_action_adapter", lambda at, c: adapter)
    agent = make_agent(ProductionAgent, ctx)
    item = PlanItemView(assigned_agent="production", action_type="create_asana_task", dry_run=False)
    res = await agent.execute(item)
    assert res.ok is True and res.summary == "did it"
    assert captured["dry_run"] is False  # item.dry_run threaded straight through
    assert captured["item"] is item


# ===========================================================================
# ResearchAgent — fact-gate (PRD §6.2, §8)
# ===========================================================================


async def test_research_observe_adds_fact_gate(monkeypatch):
    ctx = make_ctx()
    claim = mem(id=10, brand="hotcars", type=EntryType.CLAIM,
                payload={"statement": "The sky is blue", "needs_verification": True})
    ctx.store.query_responder = lambda kw: [claim]
    patch_research_llm(monkeypatch, FakeLLM(text="VERIFIED — confirmed.", citations=["http://a"]))
    agent = make_agent(ResearchAgent, ctx)  # adapters=[] -> super().observe == 0
    assert await agent.observe("hotcars") == 1


async def test_fact_gate_query_scoping():
    ctx = make_ctx()
    ctx.store.query_responder = lambda kw: []
    agent = make_agent(ResearchAgent, ctx)
    assert await agent._run_fact_gate("hotcars") == 0
    qk = ctx.store.query_log[0]
    assert qk["brand"] == "hotcars"
    assert qk["types"] == [EntryType.CLAIM]
    assert qk["payload_contains"] == {"needs_verification": True}
    assert qk["limit"] == 5  # _MAX_VERIFY_PER_RUN
    assert ctx.store.writes == []  # nothing to promote


async def test_fact_gate_promotes_verified_claim(monkeypatch):
    ctx = make_ctx()
    claim = mem(id=10, brand="hotcars", type=EntryType.CLAIM,
                payload={"statement": "Water is wet", "needs_verification": True})
    ctx.store.query_responder = lambda kw: [claim]
    patch_research_llm(monkeypatch, FakeLLM(text="VERIFIED.", citations=["http://a", "http://b"]))
    agent = make_agent(ResearchAgent, ctx)

    promoted = await agent._run_fact_gate("hotcars")

    assert promoted == 1
    assert len(ctx.store.writes) == 1
    fact = ctx.store.writes[0]
    assert fact.type == EntryType.FACT and fact.verified is True
    assert fact.brand == "hotcars"
    assert fact.payload["kind"] == "verified_fact"
    assert fact.payload["statement"] == "Water is wet"
    assert fact.payload["verified_from_claim"] == 10
    assert fact.source_urls == ["http://a", "http://b"]
    assert ctx.store.write_fact_gate == [True]  # Research is the certifying authority
    assert ctx.store.superseded == [[10]]


async def test_fact_gate_claim_key_fallback(monkeypatch):
    # statement missing but 'claim' present -> uses 'claim'.
    ctx = make_ctx()
    claim = mem(id=11, type=EntryType.CLAIM, payload={"claim": "Fallback works", "needs_verification": True})
    ctx.store.query_responder = lambda kw: [claim]
    patch_research_llm(monkeypatch, FakeLLM(text="VERIFIED", citations=[]))
    agent = make_agent(ResearchAgent, ctx)
    assert await agent._run_fact_gate("hotcars") == 1
    assert ctx.store.writes[0].payload["statement"] == "Fallback works"


async def test_fact_gate_claim_without_statement_skipped(monkeypatch):
    ctx = make_ctx()
    claim = mem(id=12, type=EntryType.CLAIM, payload={"needs_verification": True})
    ctx.store.query_responder = lambda kw: [claim]
    fake = FakeLLM(text="VERIFIED")
    patch_research_llm(monkeypatch, fake)
    agent = make_agent(ResearchAgent, ctx)
    assert await agent._run_fact_gate("hotcars") == 0
    assert fake.search_calls == []  # never verified — no statement to check
    assert ctx.store.writes == []


async def test_fact_gate_unverified_stays_claim(monkeypatch):
    ctx = make_ctx()
    claim = mem(id=13, type=EntryType.CLAIM, payload={"statement": "dubious", "needs_verification": True})
    ctx.store.query_responder = lambda kw: [claim]
    patch_research_llm(monkeypatch, FakeLLM(text="UNVERIFIED — could not confirm.", citations=["http://x"]))
    agent = make_agent(ResearchAgent, ctx)
    assert await agent._run_fact_gate("hotcars") == 0
    assert ctx.store.writes == []
    assert ctx.store.superseded == []


async def test_fact_gate_verification_unavailable(monkeypatch):
    ctx = make_ctx()
    claim = mem(id=14, type=EntryType.CLAIM, payload={"statement": "x", "needs_verification": True})
    ctx.store.query_responder = lambda kw: [claim]
    patch_research_llm(monkeypatch, FakeLLM(search_exc=AdapterUnavailable("no key")))
    agent = make_agent(ResearchAgent, ctx)
    assert await agent._run_fact_gate("hotcars") == 0
    assert ctx.store.writes == []


async def test_fact_gate_promotes_at_most_first_of_mixed(monkeypatch):
    # Two claims: first verified, second unverified -> 1 promoted, order preserved.
    ctx = make_ctx()
    c1 = mem(id=1, type=EntryType.CLAIM, payload={"statement": "yes", "needs_verification": True})
    c2 = mem(id=2, type=EntryType.CLAIM, payload={"statement": "no", "needs_verification": True})
    ctx.store.query_responder = lambda kw: [c1, c2]

    class Sequenced(FakeLLM):
        def __init__(self):
            super().__init__()
            self._n = 0

        async def web_search(self, query, *, model=None, agent="research", max_tokens=512):
            self._n += 1
            return SimpleNamespace(text="VERIFIED" if self._n == 1 else "UNVERIFIED", citations=[])

    patch_research_llm(monkeypatch, Sequenced())
    agent = make_agent(ResearchAgent, ctx)
    assert await agent._run_fact_gate("hotcars") == 1
    assert ctx.store.superseded == [[1]]


async def test_verify_first_line_logic():
    ctx = make_ctx()
    agent = make_agent(ResearchAgent, ctx)
    ok, urls = await agent._verify(FakeLLM(text="**VERIFIED**\nrest", citations=["u"]), "s")
    assert ok is True and urls == ["u"]
    assert (await agent._verify(FakeLLM(text="UNVERIFIED\nx"), "s"))[0] is False
    # 'VERIFIED' only on a later line -> the first-line judge rejects it.
    assert (await agent._verify(FakeLLM(text="Here is my answer\nVERIFIED"), "s"))[0] is False
    # markdown wrapping is stripped before judging.
    assert (await agent._verify(FakeLLM(text="## VERIFIED ##"), "s"))[0] is True


async def test_verify_unavailable_returns_false_empty():
    ctx = make_ctx()
    agent = make_agent(ResearchAgent, ctx)
    ok, urls = await agent._verify(FakeLLM(search_exc=AdapterUnavailable("down")), "s")
    assert ok is False and urls == []


# ===========================================================================
# ReportingAgent (PRD §6.6)
# ===========================================================================


def _reporting_responder(metrics, competitor):
    def responder(kw):
        if kw.get("source_system") == "rss":
            return competitor
        return metrics
    return responder


async def test_reporting_non_carbuzz_writes_digest_and_social():
    ctx = make_ctx()
    metrics = [mem(id=1, payload={"kind": "sessions_daily"}),
               mem(id=2, payload={"kind": "top_articles"})]
    ctx.store.query_responder = _reporting_responder(metrics, [])
    agent = make_agent(ReportingAgent, ctx)

    written = await agent.observe("hotcars")

    assert written == 2
    kinds = [w.payload["kind"] for w in ctx.store.writes]
    assert kinds == ["daily_digest_inputs", "social_draft"]
    digest = ctx.store.writes[0]
    assert digest.type == EntryType.REPORT
    assert digest.payload["ready"] is True  # has_sessions
    assert digest.payload["inputs"]["has_top_articles"] is True
    assert digest.payload["inputs"]["has_sessions"] is True
    assert digest.payload["inputs"]["has_competitor_coverage"] is False
    assert digest.payload["inputs"]["metric_entries"] == [1, 2]
    social = ctx.store.writes[1]
    assert social.type == EntryType.DISTRIBUTION_DRAFT
    assert social.payload["kind"] == "social_draft"


async def test_reporting_carbuzz_adds_newsletter():
    ctx = make_ctx()
    metrics = [mem(id=5, payload={"kind": "sessions_daily"})]
    ctx.store.query_responder = _reporting_responder(metrics, [])
    agent = make_agent(ReportingAgent, ctx)

    written = await agent.observe("carbuzz")

    assert written == 3
    kinds = [w.payload["kind"] for w in ctx.store.writes]
    assert kinds == ["daily_digest_inputs", "newsletter_draft", "social_draft"]
    newsletter = ctx.store.writes[1]
    assert newsletter.type == EntryType.DISTRIBUTION_DRAFT
    assert newsletter.source_system == "newsletter"


async def test_reporting_not_ready_without_sessions():
    ctx = make_ctx()
    metrics = [mem(id=1, payload={"kind": "top_articles"}),
               mem(id=2, payload={"kind": "discover_performance"}),
               mem(id=3, payload={"kind": "writer_performance"})]
    ctx.store.query_responder = _reporting_responder(metrics, [])
    agent = make_agent(ReportingAgent, ctx)
    await agent.observe("hotcars")
    inputs = ctx.store.writes[0].payload["inputs"]
    assert ctx.store.writes[0].payload["ready"] is False
    assert inputs["has_sessions"] is False
    assert inputs["has_discover"] is True
    assert inputs["has_writer_performance"] is True


async def test_reporting_competitor_query_and_flag():
    ctx = make_ctx()
    metrics = [mem(id=1, payload={"kind": "sessions_daily"})]
    competitor = [mem(brand="portfolio", type=EntryType.CONTEXT, source_system="rss",
                      payload={"kind": "competitor_coverage"})]
    ctx.store.query_responder = _reporting_responder(metrics, competitor)
    agent = make_agent(ReportingAgent, ctx)
    await agent.observe("hotcars")
    assert ctx.store.writes[0].payload["inputs"]["has_competitor_coverage"] is True
    # The competitor read is portfolio-scoped, RSS, portfolio-only, limit 1.
    comp_q = ctx.store.query_log[1]
    assert comp_q["brand"] == "portfolio"
    assert comp_q["include_portfolio"] is False
    assert comp_q["types"] == [EntryType.CONTEXT]
    assert comp_q["source_system"] == "rss"
    assert comp_q["limit"] == 1


# ===========================================================================
# OpportunityAgent (PRD §6.3)
# ===========================================================================


def _opportunity_responder(landscape, candidates):
    def responder(kw):
        if kw.get("source_system") == "similarweb":
            return landscape
        if kw.get("types") == [EntryType.CONTEXT]:
            return candidates
        return []
    return responder


async def test_shortlist_scores_and_ranks_viral_first():
    ctx = make_ctx()
    cands = [
        mem(type=EntryType.CONTEXT, source_system="tavily",
            payload={"kind": "viral_topic_candidate", "title": "Viral", "topic_id": 9}),
        mem(type=EntryType.CONTEXT, source_system="albert",
            payload={"kind": "topic_candidate", "title": "Editorial", "topic_id": 3, "source": "albert_src"}),
    ]
    ctx.store.query_responder = lambda kw: cands
    agent = make_agent(OpportunityAgent, ctx)

    assert await agent._shortlist("hotcars") == 1
    entry = ctx.store.writes[0]
    assert entry.type == EntryType.CONTEXT
    assert entry.payload["kind"] == "opportunity_shortlist"
    assert entry.payload["count"] == 2
    shortlist = entry.payload["shortlist"]
    assert shortlist[0]["title"] == "Viral" and shortlist[0]["score"] == 2.0
    assert shortlist[0]["source"] == "tavily"          # payload had no 'source' -> source_system
    assert shortlist[1]["title"] == "Editorial" and shortlist[1]["score"] == 1.0
    assert shortlist[1]["source"] == "albert_src"      # explicit payload 'source' wins


async def test_shortlist_ignores_non_candidate_kinds():
    ctx = make_ctx()
    ctx.store.query_responder = lambda kw: [mem(type=EntryType.CONTEXT, payload={"kind": "something_else"})]
    agent = make_agent(OpportunityAgent, ctx)
    assert await agent._shortlist("hotcars") == 0
    assert ctx.store.writes == []


async def test_shortlist_empty_returns_zero():
    ctx = make_ctx()
    ctx.store.query_responder = lambda kw: []
    agent = make_agent(OpportunityAgent, ctx)
    assert await agent._shortlist("hotcars") == 0
    assert ctx.store.writes == []


async def test_shortlist_caps_at_ten():
    ctx = make_ctx()
    cands = [mem(id=i, type=EntryType.CONTEXT, source_system="tavily",
                 payload={"kind": "topic_candidate", "title": f"t{i}", "topic_id": i})
             for i in range(15)]
    ctx.store.query_responder = lambda kw: cands
    agent = make_agent(OpportunityAgent, ctx)
    assert await agent._shortlist("hotcars") == 1
    entry = ctx.store.writes[0]
    assert entry.payload["count"] == 15          # counts all scored candidates
    assert len(entry.payload["shortlist"]) == 10  # but surfaces only the top 10


async def test_opportunity_observe_incorporates_landscape_and_shortlists():
    ctx = make_ctx()
    landscape = [mem(id=99, type=EntryType.METRIC, source_system="similarweb", payload={})]
    cands = [mem(type=EntryType.CONTEXT, source_system="tavily",
                 payload={"kind": "topic_candidate", "title": "t", "topic_id": 1})]
    ctx.store.query_responder = _opportunity_responder(landscape, cands)
    agent = make_agent(OpportunityAgent, ctx)  # adapters=[] -> super().observe == 0

    assert await agent.observe("hotcars") == 1
    # observe queried the similarweb landscape from memory (never a direct call).
    landscape_q = ctx.store.query_log[0]
    assert landscape_q["source_system"] == "similarweb"
    assert landscape_q["types"] == [EntryType.METRIC]


async def test_opportunity_observe_without_landscape_still_shortlists():
    ctx = make_ctx()
    cands = [mem(type=EntryType.CONTEXT, source_system="tavily",
                 payload={"kind": "viral_topic_candidate", "title": "v", "topic_id": 1})]
    ctx.store.query_responder = _opportunity_responder([], cands)
    agent = make_agent(OpportunityAgent, ctx)
    assert await agent.observe("hotcars") == 1


# ===========================================================================
# PaidMediaAgent / ProductionAgent (thin BaseAgent subclasses)
# ===========================================================================


def test_paid_media_and_production_use_base_observe():
    assert PaidMediaAgent.observe is BaseAgent.observe
    assert ProductionAgent.observe is BaseAgent.observe


def test_thin_agent_names_and_owned_tools():
    ctx = make_ctx()
    assert PaidMediaAgent.name == "paid_media"
    assert ProductionAgent.name == "production"
    assert make_agent(ProductionAgent, ctx).owned_tools == owned_tool_names("production")


async def test_paid_media_observe_runs_adapters():
    ctx = make_ctx()
    d = EntryDraft(type=EntryType.METRIC, brand="hotcars", source_agent="paid_media", payload={"k": 1})
    agent = make_agent(PaidMediaAgent, ctx, adapters=[FakeAdapter([d])])
    assert await agent.observe("hotcars") == 1
    assert ctx.store.write_many_calls[0][1] is False


# ===========================================================================
# agents package (__init__): build_agent / all_agents / run_all_observe
# ===========================================================================


def test_build_agent_known_and_unknown():
    ctx = make_ctx()
    assert isinstance(build_agent("analytics", ctx), AnalyticsAgent)
    assert isinstance(build_agent("research", ctx), ResearchAgent)
    with pytest.raises(KeyError):
        build_agent("does-not-exist", ctx)


def test_all_agents_in_declared_order():
    ctx = make_ctx()
    assert [a.name for a in all_agents(ctx)] == AGENT_ORDER


def test_agent_order_value():
    assert AGENT_ORDER == ["research", "analytics", "opportunity", "production",
                           "paid_media", "reporting"]


async def test_run_all_observe_all_ok(monkeypatch):
    fake_ctx = make_ctx()
    observed: list[tuple[str, str]] = []

    @asynccontextmanager
    async def fake_open(url=None):
        yield fake_ctx

    class FakeAg:
        def __init__(self, name):
            self.name = name

        async def observe(self, brand):
            observed.append((self.name, brand))
            return 1

    monkeypatch.setattr("switchboard.agents.RunContext.open", fake_open)
    monkeypatch.setattr("switchboard.agents.build_agent", lambda name, ctx: FakeAg(name))

    results = await run_all_observe("hotcars")
    assert results == {name: "ok" for name in AGENT_ORDER}
    assert observed == [(name, "hotcars") for name in AGENT_ORDER]


async def test_run_all_observe_isolates_one_failure(monkeypatch):
    @asynccontextmanager
    async def fake_open(url=None):
        yield make_ctx()

    class FakeAg:
        def __init__(self, name):
            self.name = name

        async def observe(self, brand):
            if self.name == "analytics":
                raise RuntimeError("boom")
            return 1

    monkeypatch.setattr("switchboard.agents.RunContext.open", fake_open)
    monkeypatch.setattr("switchboard.agents.build_agent", lambda name, ctx: FakeAg(name))

    results = await run_all_observe("hotcars")
    assert results["analytics"].startswith("error:")
    assert "boom" in results["analytics"]
    # every other agent still ran and succeeded (one failure doesn't stop the loop).
    for name in AGENT_ORDER:
        if name != "analytics":
            assert results[name] == "ok"


# ===========================================================================
# AnalyticsAgent (PRD §6.5, §16) — the largest surface
# ===========================================================================

# -- _int_cred / _flag_pct ---------------------------------------------------


def test_int_cred_parses_value():
    agent = make_agent(AnalyticsAgent, make_ctx(creds_values={"WIN": "42"}))
    assert agent._int_cred("WIN", 7) == 42


def test_int_cred_default_when_absent():
    agent = make_agent(AnalyticsAgent, make_ctx())
    assert agent._int_cred("MISSING", 9) == 9


def test_int_cred_default_when_not_int():
    agent = make_agent(AnalyticsAgent, make_ctx(creds_values={"WIN": "notanint"}))
    assert agent._int_cred("WIN", 5) == 5


def test_flag_pct_default_and_parse_and_bad():
    assert make_agent(AnalyticsAgent, make_ctx())._flag_pct() == 25.0
    assert make_agent(AnalyticsAgent, make_ctx(creds_values={"SESSION_TREND_FLAG_PCT": "40"}))._flag_pct() == 40.0
    assert make_agent(AnalyticsAgent, make_ctx(creds_values={"SESSION_TREND_FLAG_PCT": "x"}))._flag_pct() == 25.0


# -- refresh_pay_baseline (§16.4) --------------------------------------------


async def test_pay_baseline_portfolio_is_noop():
    ctx = make_ctx()
    assert await refresh_pay_baseline(ctx, "portfolio") is False
    assert ctx.governor.charges == []


async def test_pay_baseline_unknown_brand_soft_fails():
    ctx = make_ctx()
    assert await refresh_pay_baseline(ctx, "bogus") is False  # settings.brand -> KeyError


async def test_pay_baseline_bq_unavailable(monkeypatch):
    patch_bq_unavailable(monkeypatch)
    ctx = make_ctx()
    assert await refresh_pay_baseline(ctx, "hotcars") is False


async def test_pay_baseline_cap_blocks_before_query(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=999, bytes_processed=500))
    ctx = make_ctx(within=False)
    assert await refresh_pay_baseline(ctx, "hotcars") is False
    assert ctx.governor.charges == []  # never queried, never charged


async def test_pay_baseline_query_error_soft_fails(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, query_exc=RuntimeError("bq down")))
    ctx = make_ctx()
    assert await refresh_pay_baseline(ctx, "hotcars") is False


async def test_pay_baseline_charges_then_returns_false_on_no_rows(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=500, rows=[]))
    ctx = make_ctx()
    assert await refresh_pay_baseline(ctx, "hotcars") is False
    # bytes are charged before the no-rows check.
    assert ("bq_bytes", 500, "analytics") in ctx.governor.charges


async def test_pay_baseline_inserts_new_row_when_none_exists(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=800,
                                 rows=[{"usd_per_article": 100.0, "usd_per_word": 0.05, "n": 30}]))
    session = FakeSession(results=[FakeResult(scalar=None)])  # no current baseline
    ctx = make_ctx(session=session)
    assert await refresh_pay_baseline(ctx, "hotcars") is True
    added = [o for o in session.added if isinstance(o, WriterPayBaseline)]
    assert len(added) == 1
    row = added[0]
    assert row.brand == "hotcars" and row.author is None
    assert row.usd_per_article == 100.0 and row.usd_per_word == 0.05
    assert ("bq_bytes", 800, "analytics") in ctx.governor.charges
    assert session.flushes >= 1


async def test_pay_baseline_no_change_skips_insert(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=10,
                                 rows=[{"usd_per_article": 100.0, "usd_per_word": 0.05, "n": 30}]))
    current = SimpleNamespace(usd_per_article=100.0, usd_per_word=0.05)
    session = FakeSession(results=[FakeResult(scalar=current)])
    ctx = make_ctx(session=session)
    assert await refresh_pay_baseline(ctx, "hotcars") is False
    assert [o for o in session.added if isinstance(o, WriterPayBaseline)] == []


async def test_pay_baseline_inserts_when_rate_moved(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=10,
                                 rows=[{"usd_per_article": 150.0, "usd_per_word": 0.05, "n": 30}]))
    current = SimpleNamespace(usd_per_article=100.0, usd_per_word=0.05)  # >1% article move
    session = FakeSession(results=[FakeResult(scalar=current)])
    ctx = make_ctx(session=session)
    assert await refresh_pay_baseline(ctx, "hotcars") is True
    added = [o for o in session.added if isinstance(o, WriterPayBaseline)]
    assert len(added) == 1 and added[0].usd_per_article == 150.0


# -- _topic_demand (§16.3) ---------------------------------------------------


async def test_topic_demand_portfolio_zero():
    assert await make_agent(AnalyticsAgent, make_ctx())._topic_demand("portfolio") == 0


async def test_topic_demand_unknown_brand_zero():
    assert await make_agent(AnalyticsAgent, make_ctx())._topic_demand("bogus") == 0


async def test_topic_demand_cap_skips(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=999))
    agent = make_agent(AnalyticsAgent, make_ctx(within=False))
    assert await agent._topic_demand("hotcars") == 0
    assert agent.ctx.governor.charges == []


async def test_topic_demand_query_error(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, query_exc=RuntimeError("x")))
    assert await make_agent(AnalyticsAgent, make_ctx())._topic_demand("hotcars") == 0


async def test_topic_demand_no_positive_rows_charges_but_zero(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=10,
                                 rows=[{"category": "A", "articles": 20, "avg_sessions": 0, "avg_rpm": 1}]))
    agent = make_agent(AnalyticsAgent, make_ctx())
    assert await agent._topic_demand("hotcars") == 0
    assert ("bq_bytes", 10, "analytics") in agent.ctx.governor.charges


async def test_topic_demand_happy_path(monkeypatch):
    rows = [
        {"category": "SUVs", "articles": 40, "avg_sessions": 300.0, "avg_rpm": 12.0},
        {"category": "EVs", "articles": 30, "avg_sessions": 100.0, "avg_rpm": 8.0},
        {"category": "Trucks", "articles": 25, "avg_sessions": 50.0, "avg_rpm": None},
    ]
    patch_bq(monkeypatch, FakeBQ(estimate=5, bytes_processed=1000, rows=rows))
    session = FakeSession()
    ctx = make_ctx(session=session, creds_values={"TOPIC_DEMAND_TOP_N": "2"})
    agent = make_agent(AnalyticsAgent, ctx)

    assert await agent._topic_demand("hotcars") == 1
    added = [o for o in session.added if isinstance(o, BrandTopicDemand)]
    assert [a.category for a in added] == ["SUVs", "EVs"]  # top_n=2, sessions desc
    assert [a.rank for a in added] == [1, 2]
    # demand_index = category avg sessions / brand mean; mean = (300+100+50)/3 = 150.
    assert added[0].demand_index == 2.0
    assert added[0].avg_rpm == 12.0
    metric = [w for w in ctx.store.writes if w.payload.get("kind") == "topic_demand"]
    assert len(metric) == 1
    assert metric[0].payload["top"][0]["category"] == "SUVs"
    assert ("bq_bytes", 1000, "analytics") in ctx.governor.charges


# -- _writer_stats (§16.3) ---------------------------------------------------


async def test_writer_stats_portfolio_zero():
    assert await make_agent(AnalyticsAgent, make_ctx())._writer_stats("portfolio") == 0


async def test_writer_stats_bq_unavailable(monkeypatch):
    patch_bq_unavailable(monkeypatch)
    assert await make_agent(AnalyticsAgent, make_ctx())._writer_stats("hotcars") == 0


async def test_writer_stats_cap(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=999))
    assert await make_agent(AnalyticsAgent, make_ctx(within=False))._writer_stats("hotcars") == 0


async def test_writer_stats_query_error(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, query_exc=RuntimeError("x")))
    assert await make_agent(AnalyticsAgent, make_ctx())._writer_stats("hotcars") == 0


async def test_writer_stats_happy_path(monkeypatch):
    rows = []
    for _ in range(5):
        rows.append({"author": "A", "category": "cat", "intent": "feed",
                     "sessions": 1000, "cost": 200, "words": 800})
    for _ in range(6):
        rows.append({"author": "B", "category": "cat", "intent": "feed",
                     "sessions": 500, "cost": None, "words": 700})
    patch_bq(monkeypatch, FakeBQ(estimate=5, bytes_processed=2000, rows=rows))
    session = FakeSession()
    ctx = make_ctx(session=session)
    agent = make_agent(AnalyticsAgent, ctx)

    # normalize_writers keeps both (>= min_articles 5); returns the writer count.
    assert await agent._writer_stats("hotcars") == 2
    added = [o for o in session.added if isinstance(o, WriterStats)]
    assert {w.author for w in added} == {"A", "B"}
    a_row = next(w for w in added if w.author == "A")
    assert a_row.usd_per_article == 200.0        # paid writer -> real rate
    b_row = next(w for w in added if w.author == "B")
    assert b_row.usd_per_article is None          # no cost data -> honest None
    metric = [w for w in ctx.store.writes if w.payload.get("kind") == "top_writers"]
    assert len(metric) == 1 and metric[0].payload["writer_count"] == 2
    assert ("bq_bytes", 2000, "analytics") in ctx.governor.charges


# -- top_articles_by_authors (§16.3 exemplars drawer) ------------------------


async def test_top_articles_empty_authors_skips_query(monkeypatch):
    fake = FakeBQ(estimate=1, rows=[{"author": "A", "title": "t", "url": "x", "sessions": 1}])
    patch_bq(monkeypatch, fake)
    agent = make_agent(AnalyticsAgent, make_ctx())
    assert await agent.top_articles_by_authors("hotcars", []) == []
    assert fake.queries == []  # never touched BigQuery


async def test_top_articles_bq_unavailable(monkeypatch):
    patch_bq_unavailable(monkeypatch)
    agent = make_agent(AnalyticsAgent, make_ctx())
    assert await agent.top_articles_by_authors("hotcars", ["A"]) == []


async def test_top_articles_cap_blocks(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=999))
    agent = make_agent(AnalyticsAgent, make_ctx(within=False))
    assert await agent.top_articles_by_authors("hotcars", ["A"]) == []


async def test_top_articles_query_error(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, query_exc=RuntimeError("boom")))
    agent = make_agent(AnalyticsAgent, make_ctx())
    assert await agent.top_articles_by_authors("hotcars", ["A"]) == []


async def test_top_articles_happy_path(monkeypatch):
    rows = [
        {"author": "Martin", "title": "Big One", "url": "www.hotcars.com/big", "sessions": 500},
        {"author": "Hank", "title": "Second", "url": "https://www.hotcars.com/two", "sessions": 300},
        {"author": "Martin", "title": "Dup", "url": "www.hotcars.com/big", "sessions": 500},  # dupe url
        {"author": "Jared", "title": "NoUrl", "url": "", "sessions": 10},  # empty url dropped
    ]
    fake = FakeBQ(estimate=5, bytes_processed=1234, rows=rows)
    patch_bq(monkeypatch, fake)
    ctx = make_ctx()
    agent = make_agent(AnalyticsAgent, ctx)

    out = await agent.top_articles_by_authors("hotcars", ["Martin", "Hank", "Jared"], limit=5)

    # dedup by url + drop empty url; BigQuery's sessions-desc order preserved.
    assert [a["title"] for a in out] == ["Big One", "Second"]
    assert out[0]["url"] == "https://www.hotcars.com/big"  # protocol prepended
    assert out[1]["url"] == "https://www.hotcars.com/two"  # already absolute
    assert out[0]["sessions"] == 500 and out[0]["author"] == "Martin"
    assert ("bq_bytes", 1234, "analytics") in ctx.governor.charges
    _sql, params = fake.queries[0]
    assert params["brand"] == "HC" and params["authors"] == ["Martin", "Hank", "Jared"]


# -- _style_profile (§16.3) --------------------------------------------------


def _style_creds(extra=None):
    values = {"WRITER_STYLE_PROFILE_ENABLED": "1"}
    values.update(extra or {})
    return values


def _style_article_rows():
    # Two authors, two articles each -> select_exemplars can yield >= 3.
    return [
        {"author": "A", "title": "a1", "url": "http://x/a1", "sessions": 100},
        {"author": "A", "title": "a2", "url": "http://x/a2", "sessions": 90},
        {"author": "B", "title": "b1", "url": "http://x/b1", "sessions": 80},
        {"author": "B", "title": "b2", "url": "http://x/b2", "sessions": 70},
    ]


def _scraped(n):
    base = [
        {"author": "A", "url": "http://x/a1", "title": "a1", "text": "body one " * 60},
        {"author": "B", "url": "http://x/b1", "title": "b1", "text": "body two " * 60},
        {"author": "A", "url": "http://x/a2", "title": "a2", "text": "body three " * 60},
    ]
    return base[:n]


async def test_style_profile_portfolio_zero():
    assert await make_agent(AnalyticsAgent, make_ctx())._style_profile("portfolio") == 0


async def test_style_profile_flag_off_zero():
    assert await make_agent(AnalyticsAgent, make_ctx())._style_profile("hotcars") == 0


async def test_style_profile_fresh_profile_skips():
    active = SimpleNamespace(created_at=datetime.now(timezone.utc), version=3)
    session = FakeSession(results=[FakeResult(scalar=active)])
    ctx = make_ctx(session=session, creds_values=_style_creds())
    assert await make_agent(AnalyticsAgent, ctx)._style_profile("hotcars") == 0


async def test_style_profile_too_few_top_authors():
    session = FakeSession(results=[FakeResult(scalar=None), FakeResult(scalars_list=["A"])])
    ctx = make_ctx(session=session, creds_values=_style_creds())
    assert await make_agent(AnalyticsAgent, ctx)._style_profile("hotcars") == 0


async def test_style_profile_bq_cap(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=999))
    session = FakeSession(results=[FakeResult(scalar=None), FakeResult(scalars_list=["A", "B"])])
    ctx = make_ctx(session=session, creds_values=_style_creds(), within=False)
    assert await make_agent(AnalyticsAgent, ctx)._style_profile("hotcars") == 0


async def test_style_profile_bq_query_error(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, query_exc=RuntimeError("x")))
    session = FakeSession(results=[FakeResult(scalar=None), FakeResult(scalars_list=["A", "B"])])
    ctx = make_ctx(session=session, creds_values=_style_creds())
    assert await make_agent(AnalyticsAgent, ctx)._style_profile("hotcars") == 0


async def test_style_profile_too_few_exemplars(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=100, rows=[]))  # no article rows
    session = FakeSession(results=[FakeResult(scalar=None), FakeResult(scalars_list=["A", "B"])])
    ctx = make_ctx(session=session, creds_values=_style_creds())
    agent = make_agent(AnalyticsAgent, ctx)
    assert await agent._style_profile("hotcars") == 0
    assert ("bq_bytes", 100, "analytics") in ctx.governor.charges


async def test_style_profile_too_few_scraped(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=1, rows=_style_article_rows()))
    session = FakeSession(results=[FakeResult(scalar=None), FakeResult(scalars_list=["A", "B"])])
    ctx = make_ctx(session=session, creds_values=_style_creds())
    agent = make_agent(AnalyticsAgent, ctx)
    agent._scrape_exemplars = async_return(_scraped(1))  # only 1 scraped
    assert await agent._style_profile("hotcars") == 0


async def test_style_profile_llm_unavailable(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=1, rows=_style_article_rows()))
    patch_analytics_llm(monkeypatch, FakeLLM(complete_exc=AdapterUnavailable("no llm")))
    session = FakeSession(results=[FakeResult(scalar=None), FakeResult(scalars_list=["A", "B"])])
    ctx = make_ctx(session=session, creds_values=_style_creds())
    agent = make_agent(AnalyticsAgent, ctx)
    agent._scrape_exemplars = async_return(_scraped(3))
    assert await agent._style_profile("hotcars") == 0


async def test_style_profile_empty_features(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=1, rows=_style_article_rows()))
    patch_analytics_llm(monkeypatch, FakeLLM(text="{}"))  # parses to all-empty features
    session = FakeSession(results=[FakeResult(scalar=None), FakeResult(scalars_list=["A", "B"])])
    ctx = make_ctx(session=session, creds_values=_style_creds())
    agent = make_agent(AnalyticsAgent, ctx)
    agent._scrape_exemplars = async_return(_scraped(3))
    assert await agent._style_profile("hotcars") == 0


async def test_style_profile_happy_path(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=3000, rows=_style_article_rows()))
    patch_analytics_llm(monkeypatch, FakeLLM(text=_STYLE_FEATURES_JSON))
    session = FakeSession(results=[
        FakeResult(scalar=None),               # no active profile
        FakeResult(scalars_list=["A", "B"]),   # >= 2 top authors
        FakeResult(scalar=5),                  # current max version -> next is 6
        FakeResult(),                          # deactivate update (result unused)
    ])
    ctx = make_ctx(session=session, creds_values=_style_creds())
    agent = make_agent(AnalyticsAgent, ctx)
    agent._scrape_exemplars = async_return(_scraped(3))

    assert await agent._style_profile("hotcars") == 1
    added = [o for o in session.added if isinstance(o, WriterStyleProfile)]
    assert len(added) == 1
    profile = added[0]
    assert profile.brand == "hotcars" and profile.version == 6 and profile.active is True
    assert profile.source_authors == ["A", "B"]      # de-duped scraped authors
    assert profile.features["voice"] == "wry"
    metric = [w for w in ctx.store.writes if w.payload.get("kind") == "style_profile_updated"]
    assert len(metric) == 1
    assert metric[0].payload["version"] == 6 and metric[0].payload["exemplars"] == 3
    assert ("bq_bytes", 3000, "analytics") in ctx.governor.charges


# -- distill_writer_persona (§16.3) ------------------------------------------


async def test_distill_persona_unknown_brand_none():
    assert await make_agent(AnalyticsAgent, make_ctx()).distill_writer_persona("bogus", "A") is None


async def test_distill_persona_cap_none(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=999))
    agent = make_agent(AnalyticsAgent, make_ctx(within=False))
    assert await agent.distill_writer_persona("hotcars", "A") is None


async def test_distill_persona_query_error_none(monkeypatch):
    patch_bq(monkeypatch, FakeBQ(estimate=1, query_exc=RuntimeError("x")))
    assert await make_agent(AnalyticsAgent, make_ctx()).distill_writer_persona("hotcars", "A") is None


async def test_distill_persona_too_few_scraped_none(monkeypatch):
    rows = [{"author": "A", "title": "a1", "url": "http://x/a1", "sessions": 100}]
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=1, rows=rows))
    agent = make_agent(AnalyticsAgent, make_ctx())
    agent._scrape_exemplars = async_return([{"author": "A", "url": "u", "title": "t", "text": "x"}])
    assert await agent.distill_writer_persona("hotcars", "A") is None


async def test_distill_persona_llm_unavailable_none(monkeypatch):
    rows = [{"author": "A", "title": "a1", "url": "http://x/a1", "sessions": 100},
            {"author": "A", "title": "a2", "url": "http://x/a2", "sessions": 90}]
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=1, rows=rows))
    patch_analytics_llm(monkeypatch, FakeLLM(complete_exc=AdapterUnavailable("no llm")))
    agent = make_agent(AnalyticsAgent, make_ctx())
    agent._scrape_exemplars = async_return(_scraped(2))
    assert await agent.distill_writer_persona("hotcars", "A") is None


async def test_distill_persona_creates_new(monkeypatch):
    rows = [{"author": "A", "title": "a1", "url": "http://x/a1", "sessions": 100},
            {"author": "A", "title": "a2", "url": "http://x/a2", "sessions": 90}]
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=50, rows=rows))
    patch_analytics_llm(monkeypatch, FakeLLM(text=_STYLE_FEATURES_JSON))
    session = FakeSession(results=[FakeResult(scalar=None)])  # no existing persona
    ctx = make_ctx(session=session)
    agent = make_agent(AnalyticsAgent, ctx)
    agent._scrape_exemplars = async_return(_scraped(2))

    pid = await agent.distill_writer_persona("hotcars", "A")
    added = [o for o in session.added if isinstance(o, WriterPersona)]
    assert len(added) == 1
    persona = added[0]
    assert persona.kind == "writer" and persona.name == "A" and persona.author == "A"
    assert persona.enabled is True
    assert persona.features["voice"] == "wry"
    assert isinstance(pid, int) and pid == persona.id  # flush populated the id
    assert ("bq_bytes", 50, "analytics") in ctx.governor.charges


async def test_distill_persona_updates_existing(monkeypatch):
    rows = [{"author": "A", "title": "a1", "url": "http://x/a1", "sessions": 100},
            {"author": "A", "title": "a2", "url": "http://x/a2", "sessions": 90}]
    patch_bq(monkeypatch, FakeBQ(estimate=1, bytes_processed=1, rows=rows))
    patch_analytics_llm(monkeypatch, FakeLLM(text=_STYLE_FEATURES_JSON))
    existing = WriterPersona(brand="hotcars", kind="writer", name="A", author="A",
                             features={"voice": "old"}, enabled=True)
    existing.id = 42
    session = FakeSession(results=[FakeResult(scalar=existing)])
    ctx = make_ctx(session=session)
    agent = make_agent(AnalyticsAgent, ctx)
    agent._scrape_exemplars = async_return(_scraped(2))

    pid = await agent.distill_writer_persona("hotcars", "A")
    assert pid == 42
    assert existing.features["voice"] == "wry"        # updated in place
    assert existing.exemplar_refs is not None
    assert [o for o in session.added if isinstance(o, WriterPersona)] == []  # no new row


# -- _scrape_exemplars -------------------------------------------------------


async def test_scrape_exemplars_normalizes_filters_and_truncates(monkeypatch):
    import httpx
    trafilatura = pytest.importorskip("trafilatura")

    class _Resp:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text

    responses = {
        "https://good.com/a": _Resp(200, "<html>good</html>"),
        "https://short.com/b": _Resp(200, "<html>short</html>"),
        "https://bad.com/c": _Resp(404, ""),
    }
    fetched: list[str] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            fetched.append(url)
            return responses[url]

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())

    def fake_extract(html, **kwargs):
        if "good" in html:
            return "G" * 5000  # long enough to keep; over max_chars to test truncation
        if "short" in html:
            return "s" * 10    # < 400 chars -> dropped
        return None

    monkeypatch.setattr(trafilatura, "extract", fake_extract)

    agent = make_agent(AnalyticsAgent, make_ctx())  # max_chars default 2500
    exemplars = [
        {"author": "A", "title": "a", "url": "good.com/a", "sessions": 1},      # protocol-less
        {"author": "A", "title": "b", "url": "https://short.com/b", "sessions": 1},
        {"author": "B", "title": "c", "url": "https://bad.com/c", "sessions": 1},
        {"author": "C", "title": "d", "url": "", "sessions": 1},                 # empty -> skipped
    ]
    out = await agent._scrape_exemplars(exemplars)
    assert len(out) == 1
    assert out[0]["url"] == "https://good.com/a"        # normalized to absolute https
    assert out[0]["text"] == "G" * 2500                 # truncated to max_chars
    assert "https://good.com/a" in fetched


async def test_scrape_exemplars_drops_on_fetch_error(monkeypatch):
    import httpx
    pytest.importorskip("trafilatura")

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise RuntimeError("network down")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    agent = make_agent(AnalyticsAgent, make_ctx())
    out = await agent._scrape_exemplars([{"author": "A", "title": "a", "url": "https://x/a", "sessions": 1}])
    assert out == []


# -- _session_trends (§16.1) -------------------------------------------------


async def test_session_trends_portfolio_zero():
    assert await make_agent(AnalyticsAgent, make_ctx())._session_trends("portfolio") == 0


async def test_session_trends_sentinel_unavailable():
    # No SENTINEL_API_KEY -> the real SentinelClient constructor raises
    # AdapterUnavailable, which the method swallows to 0 (no network).
    agent = make_agent(AnalyticsAgent, make_ctx())
    assert await agent._session_trends("hotcars") == 0


async def test_session_trends_fetch_error(monkeypatch):
    async def boom(payload, max_pages=1):
        raise RuntimeError("sentinel down")

    patch_sentinel(monkeypatch, SimpleNamespace(traffic=boom))
    agent = make_agent(AnalyticsAgent, make_ctx(creds_values={"SENTINEL_API_KEY": "k"}))
    assert await agent._session_trends("hotcars") == 0


async def test_session_trends_happy_path_flags_and_writes(monkeypatch):
    from switchboard.session_trends import iso_week_start

    week_start = iso_week_start(date.today()) - timedelta(days=7)
    this_days = [week_start + timedelta(days=i) for i in range(7)]
    prev_days = [d - timedelta(days=7) for d in this_days]
    this_rows = [{"date": d.isoformat(), "visits": 200, "views": 240, "sessions": 180,
                  "averageEngagedDepth": 10, "averageEngagedDuration": 30} for d in this_days]
    prev_rows = [{"date": d.isoformat(), "visits": 100, "views": 120, "sessions": 90,
                  "averageEngagedDepth": 10, "averageEngagedDuration": 30} for d in prev_days]

    patch_sentinel(monkeypatch, SimpleNamespace())  # construction succeeds; _sentinel_daily faked
    ctx = make_ctx(creds_values={"SENTINEL_API_KEY": "k"})
    agent = make_agent(AnalyticsAgent, ctx)
    calls = {"n": 0}

    async def fake_daily(client, bc, start, end):
        calls["n"] += 1
        return this_rows if calls["n"] == 1 else prev_rows

    agent._sentinel_daily = fake_daily

    written = await agent._session_trends("hotcars")
    assert written >= 2  # 1 session_trends metric + >= 1 movement flag
    assert len(ctx.store.write_many_calls) == 1
    drafts = ctx.store.write_many_calls[0][0]
    assert drafts[0].type == EntryType.METRIC and drafts[0].payload["kind"] == "session_trends"
    flags = [d for d in drafts if d.type == EntryType.FLAG]
    assert flags, "visits doubled week-over-week -> a wow flag must fire"
    visits_flag = next(f for f in flags if f.payload["metric"] == "visits")
    assert visits_flag.payload["kind"] == "session_movement"
    assert visits_flag.payload["change"] == "wow"
    assert visits_flag.payload["direction"] == "up"
    assert visits_flag.payload["severity"] == "high"  # +100% >= 2x threshold(25)


async def test_sentinel_daily_builds_expected_payload():
    from switchboard.session_trends import DEFAULT_METRICS

    agent = make_agent(AnalyticsAgent, make_ctx())
    captured = {}

    class _Client:
        async def traffic(self, payload, max_pages=1):
            captured["payload"] = payload
            captured["max_pages"] = max_pages
            return [{"date": "2026-01-01"}]

    bc = agent.ctx.settings.brand("hotcars")
    out = await agent._sentinel_daily(_Client(), bc, date(2026, 1, 1), date(2026, 1, 8))
    assert out == [{"date": "2026-01-01"}]
    payload = captured["payload"]
    assert payload["granularity"] == "daily"
    assert payload["dimensions"] == ["date", "propertyId"]
    assert payload["filters"]["date"] == {"gte": "2026-01-01", "lt": "2026-01-08"}
    assert payload["filters"]["propertyId"] == {"in": ["www.hotcars.com"]}
    assert payload["metrics"] == list(DEFAULT_METRICS)
    assert captured["max_pages"] == 3


# -- _rollup -----------------------------------------------------------------


def _rollup_responder(metrics, flags):
    def responder(kw):
        if kw.get("types") == [EntryType.FLAG]:
            return flags
        return metrics
    return responder


async def test_rollup_summarizes_latest_snapshots():
    ctx = make_ctx()
    metrics = [
        mem(id=1, payload={"kind": "writer_performance", "brand_avg_spa": 123.4,
                           "writers": [{"writer": "A"}, {"writer": "B"}]}),
        mem(id=2, payload={"kind": "sessions_daily", "visits": 5000}),
        mem(id=3, payload={"kind": "discover_performance", "clicks": 700}),
        mem(id=4, payload={"kind": "top_articles", "articles": [{"title": "Top"}]}),
    ]
    flags = [mem(payload={"kind": "writer_below_index"}),
             mem(payload={"kind": "writer_below_index"}),
             mem(payload={"kind": "something_else"})]
    ctx.store.query_responder = _rollup_responder(metrics, flags)
    agent = make_agent(AnalyticsAgent, ctx)

    assert await agent._rollup("hotcars") == 1
    summary = [w for w in ctx.store.writes if w.payload.get("kind") == "analytics_summary"][0].payload
    assert summary["brand_avg_spa"] == 123.4
    assert summary["writer_count"] == 2
    assert summary["top_writer"] == {"writer": "A"}
    assert summary["at_risk_writers"] == 2
    assert summary["sessions_yesterday"] == 5000
    assert summary["discover_clicks"] == 700
    assert summary["top_article"] == {"title": "Top"}


async def test_rollup_keeps_first_occurrence_per_kind():
    ctx = make_ctx()
    metrics = [  # newest-first: the first writer_performance wins
        mem(id=1, payload={"kind": "writer_performance", "brand_avg_spa": 1.0, "writers": [{"writer": "NEW"}]}),
        mem(id=2, payload={"kind": "writer_performance", "brand_avg_spa": 9.9, "writers": [{"writer": "OLD"}]}),
    ]
    ctx.store.query_responder = _rollup_responder(metrics, [])
    agent = make_agent(AnalyticsAgent, ctx)
    await agent._rollup("hotcars")
    summary = [w for w in ctx.store.writes if w.payload.get("kind") == "analytics_summary"][0].payload
    assert summary["top_writer"] == {"writer": "NEW"}
    assert summary["brand_avg_spa"] == 1.0


async def test_rollup_empty_memory_is_safe():
    ctx = make_ctx()
    ctx.store.query_responder = _rollup_responder([], [])
    agent = make_agent(AnalyticsAgent, ctx)
    assert await agent._rollup("hotcars") == 1
    summary = [w for w in ctx.store.writes if w.payload.get("kind") == "analytics_summary"][0].payload
    assert summary["brand_avg_spa"] is None
    assert summary["writer_count"] == 0
    assert summary["top_writer"] is None
    assert summary["at_risk_writers"] == 0
    assert summary["sessions_yesterday"] is None
    assert summary["discover_clicks"] is None
    assert summary["top_article"] == {}  # (articles or [{}])[0]


# -- observe orchestration ---------------------------------------------------


async def test_analytics_observe_sums_all_paths(monkeypatch):
    ctx = make_ctx()
    agent = make_agent(AnalyticsAgent, ctx)  # adapters=[] -> super().observe == 0
    agent._rollup = async_return(1)
    agent._session_trends = async_return(2)
    agent._writer_stats = async_return(3)
    agent._topic_demand = async_return(4)
    agent._style_profile = async_return(5)

    pay_calls: list = []

    async def fake_pay(ctx_, brand, window):
        pay_calls.append((ctx_, brand, window))
        return True

    monkeypatch.setattr("switchboard.agents.analytics.refresh_pay_baseline", fake_pay)

    written = await agent.observe("hotcars")
    assert written == 1 + 2 + 3 + 4 + 5  # pay baseline is sensitive -> contributes nothing
    # pay baseline is invoked with the resolved window (default 180) and the ctx.
    assert pay_calls == [(ctx, "hotcars", 180)]
