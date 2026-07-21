"""DB-backed tests for :mod:`switchboard.trends.scout` (the Trend Scout).

The scout's ``scan`` orchestrates a lot of I/O — source adapters, HC-Viral,
BigQuery, feedparser, an LLM dossier pass, Slack. Every one of those is mocked
at its boundary here (``monkeypatch``); **nothing hits the network**. The
clustering, scoring, dedup/suppression, upsert, propose, and lifecycle logic all
run for real against Postgres.

Isolation: like ``tests/test_repo.py``'s global-mutation helpers, every scout
test runs inside a session that is **always rolled back** (:func:`_rb_session` /
the ``rb_ctx`` fixture), so even the brand-wide mutations (``expire_stale``,
``_update_lifecycle``) never persist. A belt-and-braces autouse ``_scrub``
fixture also deletes anything matched by our markers, in case a test ever
commits. Pure-logic tests build a session-less ``RunContext`` and need no DB.

pytest is ``asyncio_mode="auto"`` — async tests/fixtures need no decorator.
"""

from __future__ import annotations

import sys
import types
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import and_, delete, or_, select, text

from switchboard.adapters.base import AdapterUnavailable
from switchboard.config import TrendConfig, get_settings
from switchboard.context import RunContext
from switchboard.db.base import get_sessionmaker
from switchboard.db.enums import PORTFOLIO, EntryType, TrendStatus
from switchboard.db.models import (
    BrandTopicDemand,
    ContentJob,
    ContentPipeline,
    MemoryEntry,
    Trend,
    TrendActivity,
)
from switchboard.governor import Governor
from switchboard.interfaces import EntryDraft
from switchboard.memory.store import MemoryStore
from switchboard.trends import detector
from switchboard.trends import scout as scout_mod
from switchboard.trends.detector import DEFAULT_SCORE_WEIGHTS
from switchboard.trends.scout import TrendScout

USER = "andrew.marks@valnetinc.com"
MARKER = "utest_scout"          # brand / cluster-key / source-system prefix marker
TOK = "utestscout"              # survives tokenization → lands in scan headlines/keys


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# session / context helpers (rollback-isolated + session-less)
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def _rb_session():
    """A session whose work is ALWAYS rolled back. Skips if no DB (mirrors the
    pattern in tests/test_repo.py)."""
    maker = get_sessionmaker()
    session = maker()
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await session.close()
        pytest.skip(f"no reachable Postgres: {exc}")
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()


def _ctx_for(session) -> RunContext:
    st = get_settings()
    return RunContext(session=session, store=MemoryStore(session),
                      governor=Governor(session, st), settings=st, creds=st.creds)


def _ctx_nodb() -> RunContext:
    """A RunContext with no live session — for pure/config/creds-only methods."""
    st = get_settings()
    return RunContext(session=None, store=MemoryStore(None),
                      governor=Governor(None, st), settings=st, creds=st.creds)


@pytest.fixture
async def rb_ctx():
    """A live, rollback-isolated RunContext (skips when no Postgres)."""
    async with _rb_session() as s:
        yield _ctx_for(s)


@pytest.fixture(autouse=True)
async def _scrub():
    """Safety net: delete anything our markers touch, in a fresh committing
    context. A no-op when the DB is down (tests skip) or when rollback already
    discarded everything (the normal case)."""
    yield
    try:
        async with RunContext.open() as c:
            s = c.session
            trend_ids = select(Trend.id).where(or_(
                Trend.cluster_key.like(f"%{TOK}%"), Trend.headline.like(f"%{TOK}%"),
                Trend.cluster_key.like(f"{MARKER}%"), Trend.brand.like(f"{MARKER}%")))
            await s.execute(delete(ContentJob).where(ContentJob.pipeline_id.in_(
                select(ContentPipeline.id).where(ContentPipeline.trend_id.in_(trend_ids)))))
            await s.execute(delete(ContentPipeline).where(ContentPipeline.trend_id.in_(trend_ids)))
            await s.execute(delete(TrendActivity).where(TrendActivity.trend_id.in_(trend_ids)))
            await s.execute(delete(Trend).where(or_(
                Trend.cluster_key.like(f"%{TOK}%"), Trend.headline.like(f"%{TOK}%"),
                Trend.cluster_key.like(f"{MARKER}%"), Trend.brand.like(f"{MARKER}%"))))
            await s.execute(delete(BrandTopicDemand).where(BrandTopicDemand.brand.like(f"{MARKER}%")))
            await s.execute(delete(MemoryEntry).where(MemoryEntry.source_system.like(f"{MARKER}%")))
            await s.execute(delete(MemoryEntry).where(and_(
                MemoryEntry.source_agent == "trend_scout",
                MemoryEntry.payload["headline"].astext.like(f"%{TOK}%"))))
            await s.commit()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# small builders
# --------------------------------------------------------------------------- #
def _afn(value):
    """Build an async function returning ``value`` (absorbs any args)."""
    async def _f(*a, **k):
        return value
    return _f


class _Rec:
    """Async recorder — stands in for collect_dossier / notify_trend_event."""

    def __init__(self, ret=None, exc: Exception | None = None) -> None:
        self.calls: list = []
        self._ret = ret
        self._exc = exc

    async def __call__(self, *a, **k):
        self.calls.append((a, k))
        if self._exc:
            raise self._exc
        return self._ret

    @property
    def count(self) -> int:
        return len(self.calls)


def _cfg(**over) -> TrendConfig:
    base = dict(enabled=True, scan_interval_min=120, score_threshold=60.0, min_sources=2,
                max_open_pipelines=5, ttl_hours=48, dedup_days=5, auto_dossier=True,
                base_query="automotive industry news", watchlist=(),
                default_content_types=("article", "social_post"))
    base.update(over)
    return TrendConfig(**base)


def _items(n: int = 3, *, oem: str = "tesla", topic: str = "recall investigation battery fire",
           source_prefix: str = "outlet") -> list[dict]:
    """A cluster's worth of near-identical, breaking, timestamped signals from
    ``n`` distinct outlets — enough to cluster and score."""
    now = _now()
    out = []
    for i in range(n):
        out.append({
            "origin": "tavily", "source": f"{source_prefix}{i}",
            "title": f"{oem.title()} {TOK} {topic}",
            "url": f"https://ex.example/{TOK}/{oem}/{i}",
            "published_at": (now - timedelta(minutes=30 * i)).isoformat(), "snippet": "",
        })
    return out


def _cluster_key(items: list[dict]) -> str:
    clusters = detector.cluster_signals(items)
    c = max(clusters, key=lambda x: len(x.items))
    return c.cluster_key()


def _wire(scout: TrendScout, monkeypatch, *, signals, our=None, hc=None, dr=None,
          neigh=None, session_mom=None, demand=None, lifecycle=0):
    """Stub every network/DB-heavy fetcher so ``scan`` runs offline & deterministic.
    The clustering/scoring/upsert/propose paths stay real."""
    monkeypatch.setattr(scout, "_pull_sources", _afn(2))
    monkeypatch.setattr(scout, "_signals_from_memory", _afn(signals))
    monkeypatch.setattr(scout, "_our_titles", _afn(list(our or [])))
    monkeypatch.setattr(scout, "_hc_viral_titles", _afn(list(hc or [])))
    monkeypatch.setattr(scout, "_daily_reporting_titles", _afn(list(dr or [])))
    monkeypatch.setattr(scout, "_neighborhood_trends", _afn(list(neigh or [])))
    monkeypatch.setattr(scout, "_session_momentum", _afn(dict(session_mom or {})))
    monkeypatch.setattr(scout, "_demand_terms", _afn(list(demand or [])))
    monkeypatch.setattr(scout, "_update_lifecycle", _afn(lifecycle))
    monkeypatch.setattr("switchboard.trends.weights.load_effective",
                        _afn(dict(DEFAULT_SCORE_WEIGHTS)))


# =========================================================================== #
# pure helpers — no DB
# =========================================================================== #
def test_evidence_trims_to_five_keys_with_defaults():
    trimmed = scout_mod._evidence({"origin": "tavily", "source": "s", "title": "t",
                                   "url": "u", "published_at": "p", "extra": "drop"})
    assert trimmed == {"origin": "tavily", "source": "s", "title": "t", "url": "u",
                       "published_at": "p"}
    # missing keys default to ""
    assert scout_mod._evidence({}) == {"origin": "", "source": "", "title": "",
                                       "url": "", "published_at": ""}


def _mk_cluster(title: str) -> detector.Cluster:
    it = {"title": title, "snippet": "", "url": "u", "source": "s", "published_at": ""}
    return detector.Cluster(items=[it], token_set=detector.tokens(title))


def test_cluster_signals_prefers_real_session_momentum():
    scout = TrendScout(_ctx_nodb())
    cluster = _mk_cluster(f"tesla {TOK} recall investigation battery")
    q, r = scout._cluster_signals(cluster, [], {"tesla": 0.42})
    assert q == 0.42 and r is None


def test_cluster_signals_session_beats_same_topic_proxy():
    scout = TrendScout(_ctx_nodb())
    cluster = _mk_cluster(f"tesla {TOK} recall investigation battery")
    neigh = [{"tokens": set(cluster.token_set), "oems": ("tesla",),
              "state": "rising", "age_days": 1}]
    # session signal present ⇒ proxy is NOT consulted even though a same-topic
    # rising neighbour exists.
    q, r = scout._cluster_signals(cluster, neigh, {"tesla": -0.3})
    assert q == -0.3


def test_cluster_signals_proxy_fallback_when_no_session():
    scout = TrendScout(_ctx_nodb())
    cluster = _mk_cluster(f"tesla {TOK} recall investigation battery")
    neigh = [{"tokens": set(cluster.token_set), "oems": ("tesla",),
              "state": "rising", "age_days": 1}]
    q, r = scout._cluster_signals(cluster, neigh, {})   # no session data
    assert q == scout_mod._STATE_MOMENTUM["rising"] == 0.6
    assert r is None


def test_cluster_signals_adjacent_theme_fatigue():
    scout = TrendScout(_ctx_nodb())
    cluster = _mk_cluster(f"tesla {TOK} recall investigation battery")  # 5 tokens
    # 3 shared + 2 novel tokens, no shared OEM ⇒ similarity in [0.3, 0.6): adjacent.
    nb = {"tokens": {"tesla", "recall", "investigation", "alpha", "beta"},
          "oems": (), "state": "declining", "age_days": 5}
    q, r = scout._cluster_signals(cluster, [nb], {})
    assert q is None
    sim = detector.topic_similarity(cluster, nb["tokens"], nb["oems"])
    assert 0.3 <= sim < 0.6
    assert r == pytest.approx(round(sim * scout_mod._STATE_DECLINE["declining"], 3))


def test_cluster_signals_young_adjacent_theme_does_not_fatigue():
    scout = TrendScout(_ctx_nodb())
    cluster = _mk_cluster(f"tesla {TOK} recall investigation battery")
    nb = {"tokens": {"tesla", "recall", "investigation", "alpha", "beta"},
          "oems": (), "state": "declining", "age_days": 1}   # too young (< 3 days)
    q, r = scout._cluster_signals(cluster, [nb], {})
    assert (q, r) == (None, None)


# =========================================================================== #
# scan() — guard rails
# =========================================================================== #
async def test_scan_disabled_returns_early(monkeypatch):
    scout = TrendScout(_ctx_nodb())
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(enabled=False))
    assert await scout.scan("portfolio") == {"enabled": False}


async def test_scan_invalid_brand_scope_refused():
    scout = TrendScout(_ctx_nodb())   # real (enabled) config, no DB touched
    res = await scout.scan("not-a-brand")
    assert res["enabled"] is True
    assert "error" in res and "invalid brand" in res["error"]


# =========================================================================== #
# scan() — full flow
# =========================================================================== #
async def test_scan_detects_and_proposes_a_hot_trend(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends",
                        _cfg(score_threshold=20.0, min_sources=2, max_open_pipelines=9999))
    collect = _Rec(ret={})
    notify = _Rec(ret=True)
    monkeypatch.setattr(scout_mod, "collect_dossier", collect)
    monkeypatch.setattr(scout_mod, "notify_trend_event", notify)

    items = _items(3)
    _wire(scout, monkeypatch, signals=items)

    summary = await scout.scan("hotcars")

    assert summary["enabled"] is True
    assert summary["signals"] == 3
    assert summary["clusters"] == 1
    assert summary["sources_pulled"] == 2
    assert summary["new_trends"] == 1
    assert summary["proposed"] == 1
    assert summary["lifecycle_declined"] == 0
    assert collect.count == 1                 # dossier built for the proposal
    assert notify.count == 1                  # trigger_requested notification

    trend = await scout.trends.find_by_cluster_key(_cluster_key(items), brand="hotcars")
    assert trend is not None
    assert trend.status == TrendStatus.PROPOSED.value
    assert TOK in trend.headline
    pipes = (await rb_ctx.session.execute(
        select(ContentPipeline).where(ContentPipeline.trend_id == trend.id))).scalars().all()
    assert len(pipes) == 1
    assert pipes[0].status == "pending_approval"


async def test_scan_below_threshold_detects_but_does_not_propose(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(score_threshold=99.0))
    _wire(scout, monkeypatch, signals=_items(3))

    summary = await scout.scan("hotcars")
    assert summary["new_trends"] == 1
    assert summary["proposed"] == 0


async def test_scan_skips_cluster_below_min_sources(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(min_sources=2, score_threshold=1.0))
    _wire(scout, monkeypatch, signals=_items(1))   # one outlet only

    summary = await scout.scan("hotcars")
    assert summary["clusters"] == 1
    assert summary["new_trends"] == 0
    assert summary["proposed"] == 0


async def test_scan_records_corroboration_from_other_monitors(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(score_threshold=99.0))
    match = f"Tesla {TOK} recall investigation battery"
    _wire(scout, monkeypatch, signals=_items(3), hc=[match], dr=[match])

    summary = await scout.scan("hotcars")
    assert summary["corroborated"] == 1

    trend = await scout.trends.find_by_cluster_key(_cluster_key(_items(3)), brand="hotcars")
    corr = (trend.entities or {}).get("corroborated_by", [])
    assert "hc_viral_hits" in corr and "daily_reporting" in corr


async def test_scan_covered_by_us_sets_flag_true(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(score_threshold=99.0))
    _wire(scout, monkeypatch, signals=_items(3),
          our=[f"Tesla {TOK} recall investigation battery fire"])

    await scout.scan("hotcars")
    trend = await scout.trends.find_by_cluster_key(_cluster_key(_items(3)), brand="hotcars")
    assert trend.covered_by_us is True


async def test_scan_uncovered_sets_flag_false(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(score_threshold=99.0))
    _wire(scout, monkeypatch, signals=_items(3), our=["totally unrelated zebra headline"])

    await scout.scan("hotcars")
    trend = await scout.trends.find_by_cluster_key(_cluster_key(_items(3)), brand="hotcars")
    assert trend.covered_by_us is False


async def test_scan_suppressed_by_recent_dismissed_twin(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(dedup_days=5, score_threshold=1.0))
    items = _items(3)
    ck = _cluster_key(items)
    # a dismissed twin seen just now → within the dedup window → upsert returns None.
    rb_ctx.session.add(Trend(brand="hotcars", cluster_key=ck, headline=f"{TOK} twin",
                             status="dismissed", score=0.0, last_seen_at=_now()))
    await rb_ctx.session.flush()
    _wire(scout, monkeypatch, signals=items)

    summary = await scout.scan("hotcars")
    assert summary["suppressed"] == 1
    assert summary["new_trends"] == 0
    assert summary["proposed"] == 0


async def test_scan_refreshes_open_twin_and_suppressed_gate_blocks_proposal(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(score_threshold=1.0, max_open_pipelines=9999))
    items = _items(3)
    ck = _cluster_key(items)
    # an OPEN twin already flagged as suppressed → refreshed in place, and the
    # opportunity-suppression gate (`not trend.suppressed`) blocks re-proposal.
    rb_ctx.session.add(Trend(brand="hotcars", cluster_key=ck, headline=f"{TOK} open twin",
                             status="detected", score=0.0, suppressed=True, last_seen_at=_now()))
    await rb_ctx.session.flush()
    _wire(scout, monkeypatch, signals=items)

    summary = await scout.scan("hotcars")
    assert summary["updated_trends"] == 1
    assert summary["new_trends"] == 0
    assert summary["proposed"] == 0


async def test_scan_already_proposed_twin_is_not_reproposed(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(score_threshold=1.0, max_open_pipelines=9999))
    items = _items(3)
    ck = _cluster_key(items)
    rb_ctx.session.add(Trend(brand="hotcars", cluster_key=ck, headline=f"{TOK} proposed twin",
                             status="proposed", score=0.0, last_seen_at=_now()))
    await rb_ctx.session.flush()
    _wire(scout, monkeypatch, signals=items)

    summary = await scout.scan("hotcars")
    assert summary["updated_trends"] == 1
    assert summary["proposed"] == 0   # status != detected → gate skips it


async def test_scan_proposals_capped_per_scan(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(score_threshold=20.0, max_open_pipelines=9999))
    monkeypatch.setattr(scout_mod, "collect_dossier", _Rec(ret={}))
    monkeypatch.setattr(scout_mod, "notify_trend_event", _Rec(ret=True))
    monkeypatch.setattr(scout_mod, "_MAX_NEW_PROPOSALS_PER_SCAN", 1)   # cap: one proposal/scan
    # two independent, non-merging clusters (different OEM + topic).
    a = _items(3, oem="tesla", topic="recall investigation battery fire")
    b = _items(3, oem="ford", topic="bronco raptor unveiled price specs", source_prefix="press")
    _wire(scout, monkeypatch, signals=a + b)

    summary = await scout.scan("hotcars")
    assert summary["new_trends"] == 2
    assert summary["proposed"] == 1    # proposals_left exhausted after the first


async def test_scan_proposal_cap_reached_flags_only(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    # max_open_pipelines=0 ⇒ _propose hits the cap branch: writes a flag, no pipeline.
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(score_threshold=20.0, max_open_pipelines=0))
    notify = _Rec(ret=True)
    monkeypatch.setattr(scout_mod, "collect_dossier", _Rec(ret={}))
    monkeypatch.setattr(scout_mod, "notify_trend_event", notify)
    _wire(scout, monkeypatch, signals=_items(3))

    summary = await scout.scan("hotcars")
    assert summary["new_trends"] == 1
    assert summary["proposed"] == 0
    assert notify.count == 0            # cap path never notifies
    trend = await scout.trends.find_by_cluster_key(_cluster_key(_items(3)), brand="hotcars")
    assert trend.status == TrendStatus.DETECTED.value   # not advanced to proposed


# =========================================================================== #
# _pull_sources
# =========================================================================== #
async def test_pull_sources_counts_nonempty_and_isolates_failures(rb_ctx, monkeypatch):
    class _OK:
        name = "ok"

        def __init__(self, ctx):
            self.ctx = ctx

        async def observe(self, brand):
            assert brand == PORTFOLIO
            return [EntryDraft(type=EntryType.CONTEXT, brand=PORTFOLIO, source_agent="research",
                               source_system=f"{MARKER}_src",
                               payload={"kind": "trend_signals", "items": [{"title": TOK}]},
                               ttl_seconds=3600)]

    class _Empty(_OK):
        name = "empty"

        async def observe(self, brand):
            return []

    class _Boom(_OK):
        name = "boom"

        async def observe(self, brand):
            raise RuntimeError("source exploded")

    monkeypatch.setattr(scout_mod, "_SOURCE_ADAPTERS", (_OK, _Empty, _Boom))
    scout = TrendScout(rb_ctx)

    pulled = await scout._pull_sources()
    assert pulled == 1     # only _OK produced drafts; _Empty skipped; _Boom isolated

    # the _OK draft was actually persisted
    rows = await rb_ctx.store.query(brand=PORTFOLIO, types=[EntryType.CONTEXT],
                                    source_system=f"{MARKER}_src", limit=5)
    assert len(rows) == 1
    assert rows[0].payload["kind"] == "trend_signals"


# =========================================================================== #
# _signals_from_memory
# =========================================================================== #
async def test_signals_from_memory_flattens_and_filters(rb_ctx):
    scout = TrendScout(rb_ctx)
    await rb_ctx.store.write(EntryDraft(
        type=EntryType.CONTEXT, brand=PORTFOLIO, source_agent="research",
        source_system=f"{MARKER}_sig",
        payload={"kind": "trend_signals", "items": [
            {"title": f"{TOK} A", "url": "u1"},   # kept (title)
            {"url": f"{TOK}-u2"},                  # kept (url only)
            {"snippet": f"{TOK}-dropme"},          # dropped (no title, no url)
        ]}))
    await rb_ctx.store.write(EntryDraft(
        type=EntryType.CONTEXT, brand=PORTFOLIO, source_agent="research",
        source_system=f"{MARKER}_cov",
        payload={"kind": "competitor_coverage", "items": [
            {"source": "AutoBlog", "title": f"{TOK} CT", "link": "L1", "published": "2026-07-20"},
        ]}))

    items = await scout._signals_from_memory()
    titles = [i.get("title") for i in items]
    urls = [i.get("url") for i in items]
    assert f"{TOK} A" in titles
    assert f"{TOK}-u2" in urls
    # the no-title/no-url signal was filtered out
    assert not any(i.get("snippet") == f"{TOK}-dropme" for i in items)
    # competitor_coverage was normalized (link→url, origin=rss, published→published_at)
    ct = next(i for i in items if i.get("title") == f"{TOK} CT")
    assert ct == {"origin": "rss", "source": "AutoBlog", "title": f"{TOK} CT",
                  "url": "L1", "published_at": "2026-07-20", "snippet": ""}


# =========================================================================== #
# _our_titles  (feedparser mocked)
# =========================================================================== #
async def test_our_titles_reads_each_brand_feed(monkeypatch):
    fake = types.ModuleType("feedparser")

    def parse(url):
        return SimpleNamespace(entries=[SimpleNamespace(title="T1"),
                                        SimpleNamespace(title="T2")])

    fake.parse = parse
    monkeypatch.setitem(sys.modules, "feedparser", fake)

    scout = TrendScout(_ctx_nodb())
    titles = await scout._our_titles()
    assert titles == ["T1", "T2", "T1", "T2", "T1", "T2"]   # 3 brands × 2 entries


async def test_our_titles_uses_env_override_feed(monkeypatch):
    fake = types.ModuleType("feedparser")
    seen: list[str] = []

    def parse(url):
        seen.append(url)
        return SimpleNamespace(entries=[SimpleNamespace(title="X")])

    fake.parse = parse
    monkeypatch.setitem(sys.modules, "feedparser", fake)

    ctx = _ctx_nodb()
    orig = ctx.creds.resolve

    def fake_resolve(key, **kw):
        if key.startswith("OUR_NEWS_FEED_"):
            return f"https://override.example/{key.lower()}.xml"
        return orig(key, **kw)

    monkeypatch.setattr(ctx.creds, "resolve", fake_resolve)
    scout = TrendScout(ctx)
    titles = await scout._our_titles()
    assert titles == ["X", "X", "X"]                          # 3 brands
    assert all(u.startswith("https://override.example/") for u in seen)


async def test_our_titles_soft_fails_per_feed(monkeypatch):
    fake = types.ModuleType("feedparser")

    def parse(url):
        raise RuntimeError("feed down")

    fake.parse = parse
    monkeypatch.setitem(sys.modules, "feedparser", fake)

    scout = TrendScout(_ctx_nodb())
    assert await scout._our_titles() == []


async def test_our_titles_without_feedparser_returns_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "feedparser", None)   # import raises ImportError
    scout = TrendScout(_ctx_nodb())
    assert await scout._our_titles() == []


# =========================================================================== #
# _hc_viral_titles  (HCViralClient mocked)
# =========================================================================== #
def _patch_hc_key(monkeypatch, ctx, present: bool):
    orig = ctx.creds.resolve

    def fake(key, **kw):
        if key == "HC_VIRAL_HITS_API_KEY":
            return "hc-key" if present else None
        return orig(key, **kw)

    monkeypatch.setattr(ctx.creds, "resolve", fake)


async def test_hc_viral_titles_no_key_returns_empty(monkeypatch):
    ctx = _ctx_nodb()
    _patch_hc_key(monkeypatch, ctx, present=False)
    scout = TrendScout(ctx)
    assert await scout._hc_viral_titles() == []


async def test_hc_viral_titles_topics_surface(monkeypatch):
    ctx = _ctx_nodb()
    _patch_hc_key(monkeypatch, ctx, present=True)

    class _Client:
        def __init__(self, base, key):
            pass

        async def list_topics(self, brand):
            return [{"title": f"top-{brand}"}, {"title": ""}]   # blanks filtered

        async def list_drafts(self, brand, status=None):   # pragma: no cover
            raise AssertionError("drafts must not be consulted when topics work")

    monkeypatch.setattr("switchboard.adapters.clients.hcviral.HCViralClient", _Client)
    scout = TrendScout(ctx)
    assert await scout._hc_viral_titles() == ["top-hotcars", "top-topspeed"]


async def test_hc_viral_titles_falls_back_to_drafts(monkeypatch):
    ctx = _ctx_nodb()
    _patch_hc_key(monkeypatch, ctx, present=True)

    class _Client:
        def __init__(self, base, key):
            pass

        async def list_topics(self, brand):
            raise RuntimeError("topics endpoint absent")

        async def list_drafts(self, brand, status=None):
            return [{"title": f"draft-{brand}"}]

    monkeypatch.setattr("switchboard.adapters.clients.hcviral.HCViralClient", _Client)
    scout = TrendScout(ctx)
    assert await scout._hc_viral_titles() == ["draft-hotcars", "draft-topspeed"]


async def test_hc_viral_titles_both_endpoints_fail_soft_empty(monkeypatch):
    ctx = _ctx_nodb()
    _patch_hc_key(monkeypatch, ctx, present=True)

    class _Client:
        def __init__(self, base, key):
            pass

        async def list_topics(self, brand):
            raise RuntimeError("no topics")

        async def list_drafts(self, brand, status=None):
            raise RuntimeError("no drafts")

    monkeypatch.setattr("switchboard.adapters.clients.hcviral.HCViralClient", _Client)
    scout = TrendScout(ctx)
    assert await scout._hc_viral_titles() == []


# =========================================================================== #
# _daily_reporting_titles
# =========================================================================== #
async def test_daily_reporting_titles_reads_shared_memory(rb_ctx):
    scout = TrendScout(rb_ctx)
    await rb_ctx.store.write(EntryDraft(
        type=EntryType.CONTEXT, brand=PORTFOLIO, source_agent="feeder",
        source_system=f"{MARKER}_dr",
        payload={"kind": "daily_report_trends", "items": [
            {"title": f"{TOK} D1"}, {"topic": f"{TOK} D2"},
            {"headline": f"{TOK} D3"}, {"note": "no title-ish key"},
        ]}))
    titles = await scout._daily_reporting_titles()
    for expected in (f"{TOK} D1", f"{TOK} D2", f"{TOK} D3"):
        assert expected in titles


async def test_daily_reporting_titles_soft_fails_on_query_error(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(rb_ctx.store, "query", _afn_raises(RuntimeError("boom")))
    assert await scout._daily_reporting_titles() == []


def _afn_raises(exc):
    async def _f(*a, **k):
        raise exc
    return _f


# =========================================================================== #
# _neighborhood_trends
# =========================================================================== #
async def test_neighborhood_trends_shape(rb_ctx):
    scout = TrendScout(rb_ctx)
    b = f"{MARKER}_nb"
    rb_ctx.session.add(Trend(brand=b, cluster_key=f"{MARKER}_nb1", headline=f"Tesla {TOK} recall",
                             status="detected", score=1.0, state="rising",
                             entities={"oems": ["tesla"]},
                             created_at=_now() - timedelta(days=2)))
    await rb_ctx.session.flush()

    neigh = await scout._neighborhood_trends(b)
    assert len(neigh) == 1
    row = neigh[0]
    assert set(row.keys()) == {"tokens", "oems", "state", "age_days"}
    assert "tesla" in row["tokens"]
    assert row["oems"] == ("tesla",)
    assert row["state"] == "rising"
    assert row["age_days"] >= 2


async def test_neighborhood_trends_soft_fails(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.trends, "list", _afn_raises(RuntimeError("db oops")))
    assert await scout._neighborhood_trends("hotcars") == []


# =========================================================================== #
# _demand_terms
# =========================================================================== #
async def test_demand_terms_weights_depluralizes_and_filters(rb_ctx):
    scout = TrendScout(rb_ctx)
    b = f"{MARKER}_dem"
    rows = [
        BrandTopicDemand(brand=b, category="Electric Motorcycles", articles=10,
                         avg_sessions=100.0, demand_index=2.0, rank=1, window_days=90),
        BrandTopicDemand(brand=b, category="SUVs", articles=5, avg_sessions=50.0,
                         demand_index=1.0, rank=2, window_days=90),
        BrandTopicDemand(brand=b, category="Car News", articles=3, avg_sessions=25.0,
                         demand_index=0.5, rank=3, window_days=90),   # both words generic/short
    ]
    for r in rows:
        rb_ctx.session.add(r)
    await rb_ctx.session.flush()

    terms = dict(await scout._demand_terms(b))
    assert terms["electric"] == pytest.approx(1.0)        # 2.0 / 2.0
    assert terms["motorcycle"] == pytest.approx(1.0)      # de-pluralized
    assert terms["suvs"] == pytest.approx(0.5)            # 1.0 / 2.0; len 4 → not de-pluralized
    assert "news" not in terms and "car" not in terms     # generic words dropped


async def test_demand_terms_empty_when_no_rows(rb_ctx):
    scout = TrendScout(rb_ctx)
    assert await scout._demand_terms(f"{MARKER}_dem_none_{uuid4().hex}") == []


# =========================================================================== #
# _session_momentum  (BigQuery mocked; never hits the network)
# =========================================================================== #
async def test_session_momentum_client_unavailable(monkeypatch):
    class _Boom:
        def __init__(self, sa):
            raise AdapterUnavailable("no service account")

    monkeypatch.setattr("switchboard.adapters.clients.bigquery.BigQueryClient", _Boom)
    scout = TrendScout(_ctx_nodb())
    assert await scout._session_momentum() == {}


async def test_session_momentum_success_charges_and_computes(monkeypatch):
    rows = [
        {"title": "Tesla Model 3 review", "sessions": 50, "week": "202601"},
        {"title": "Tesla Model Y first drive", "sessions": 50, "week": "202601"},
        {"title": "Tesla Cybertruck update", "sessions": 150, "week": "202602"},
        {"title": "Tesla Roadster news", "sessions": 150, "week": "202602"},
    ]

    class _BQ:
        def __init__(self, sa):
            pass

        async def estimate_bytes(self, sql, params):
            return 1234

        async def query(self, sql, params):
            return SimpleNamespace(rows=rows, bytes_processed=1234)

    monkeypatch.setattr("switchboard.adapters.clients.bigquery.BigQueryClient", _BQ)
    ctx = _ctx_nodb()
    monkeypatch.setattr(ctx.governor, "within_caps", _afn(True))
    charge = _Rec()
    monkeypatch.setattr(ctx.governor, "charge", charge)

    scout = TrendScout(ctx)
    out = await scout._session_momentum()
    assert "tesla" in out and out["tesla"] > 0   # sessions rose recent-half
    assert charge.count == 1                      # bq_bytes charged


async def test_session_momentum_cap_exceeded(monkeypatch):
    class _BQ:
        def __init__(self, sa):
            pass

        async def estimate_bytes(self, sql, params):
            return 10 ** 15

        async def query(self, sql, params):   # pragma: no cover
            raise AssertionError("query must not run when the cap is hit")

    monkeypatch.setattr("switchboard.adapters.clients.bigquery.BigQueryClient", _BQ)
    ctx = _ctx_nodb()
    monkeypatch.setattr(ctx.governor, "within_caps", _afn(False))
    scout = TrendScout(ctx)
    assert await scout._session_momentum() == {}


async def test_session_momentum_query_failure_soft_empty(monkeypatch):
    class _BQ:
        def __init__(self, sa):
            pass

        async def estimate_bytes(self, sql, params):
            return 10

        async def query(self, sql, params):
            raise RuntimeError("bq unreachable")

    monkeypatch.setattr("switchboard.adapters.clients.bigquery.BigQueryClient", _BQ)
    ctx = _ctx_nodb()
    monkeypatch.setattr(ctx.governor, "within_caps", _afn(True))
    scout = TrendScout(ctx)
    assert await scout._session_momentum() == {}


# =========================================================================== #
# _propose  (direct)
# =========================================================================== #
async def _seed_trend(session, *, brand="hotcars", status="detected", headline=None,
                      score=80.0, **kw) -> Trend:
    t = Trend(brand=brand, cluster_key=f"{MARKER}_{uuid4().hex[:10]}",
              headline=headline or f"{TOK} tesla recall investigation", status=status,
              score=score, evidence=[{"url": "https://ex.example/e1"}],
              entities={"oems": ["tesla"]}, **kw)
    session.add(t)
    await session.flush()
    return t


async def test_propose_happy_creates_pipeline_and_notifies(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(max_open_pipelines=9999, auto_dossier=True))
    collect = _Rec(ret={})
    notify = _Rec(ret=True)
    monkeypatch.setattr(scout_mod, "collect_dossier", collect)
    monkeypatch.setattr(scout_mod, "notify_trend_event", notify)

    trend = await _seed_trend(rb_ctx.session)
    assert await scout._propose(trend) is True
    assert trend.status == TrendStatus.PROPOSED.value
    assert collect.count == 1 and notify.count == 1
    pipes = (await rb_ctx.session.execute(
        select(ContentPipeline).where(ContentPipeline.trend_id == trend.id))).scalars().all()
    assert len(pipes) == 1 and pipes[0].status == "pending_approval"


async def test_propose_auto_dossier_off_skips_dossier(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(max_open_pipelines=9999, auto_dossier=False))
    collect = _Rec(ret={})
    monkeypatch.setattr(scout_mod, "collect_dossier", collect)
    monkeypatch.setattr(scout_mod, "notify_trend_event", _Rec(ret=True))

    trend = await _seed_trend(rb_ctx.session)
    assert await scout._propose(trend) is True
    assert collect.count == 0                         # dossier skipped
    assert trend.status == TrendStatus.PROPOSED.value


async def test_propose_dossier_failure_is_swallowed(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(max_open_pipelines=9999, auto_dossier=True))
    monkeypatch.setattr(scout_mod, "collect_dossier", _Rec(exc=RuntimeError("LLM down")))
    monkeypatch.setattr(scout_mod, "notify_trend_event", _Rec(ret=True))

    trend = await _seed_trend(rb_ctx.session)
    assert await scout._propose(trend) is True        # dossier error non-fatal
    assert trend.status == TrendStatus.PROPOSED.value


async def test_propose_invalid_content_types_bails(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends",
                        _cfg(max_open_pipelines=9999, default_content_types=("bogus",)))
    collect = _Rec(ret={})
    monkeypatch.setattr(scout_mod, "collect_dossier", collect)
    monkeypatch.setattr(scout_mod, "notify_trend_event", _Rec(ret=True))

    trend = await _seed_trend(rb_ctx.session)
    assert await scout._propose(trend) is False
    assert collect.count == 0
    pipes = (await rb_ctx.session.execute(
        select(ContentPipeline).where(ContentPipeline.trend_id == trend.id))).scalars().all()
    assert pipes == []


async def test_propose_cap_reached_writes_flag_only(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(max_open_pipelines=0))
    monkeypatch.setattr(scout_mod, "collect_dossier", _Rec(ret={}))
    notify = _Rec(ret=True)
    monkeypatch.setattr(scout_mod, "notify_trend_event", notify)

    trend = await _seed_trend(rb_ctx.session)
    assert await scout._propose(trend) is False
    assert notify.count == 0
    assert trend.status == TrendStatus.DETECTED.value
    # a "cap reached" flag was surfaced to the planner
    flags = await rb_ctx.store.query(brand="hotcars", types=[EntryType.FLAG],
                                     payload_contains={"kind": "competitor_trend",
                                                       "trend_id": trend.id}, limit=5)
    assert any((f.payload or {}).get("note") == "proposal cap reached" for f in flags)


async def test_propose_duplicate_open_pipeline_marks_proposed(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    monkeypatch.setattr(scout.ctx.settings, "trends", _cfg(max_open_pipelines=9999, auto_dossier=False))
    monkeypatch.setattr(scout_mod, "collect_dossier", _Rec(ret={}))
    monkeypatch.setattr(scout_mod, "notify_trend_event", _Rec(ret=True))

    trend = await _seed_trend(rb_ctx.session)
    rb_ctx.session.add(ContentPipeline(trend_id=trend.id, brand="hotcars",
                                       status="pending_approval", content_types=["article"]))
    await rb_ctx.session.flush()

    # create() refuses the second open pipeline → de-facto proposed, returns False.
    assert await scout._propose(trend) is False
    assert trend.status == TrendStatus.PROPOSED.value


# =========================================================================== #
# _write_flag / _write_lifecycle_flag
# =========================================================================== #
async def test_write_flag_breaking_severity_and_supersede(rb_ctx):
    scout = TrendScout(rb_ctx)
    trend = await _seed_trend(rb_ctx.session, headline=f"{TOK} Tesla recall investigation")

    await scout._write_flag(trend)
    first = await rb_ctx.store.query(brand="hotcars", types=[EntryType.FLAG],
                                     payload_contains={"kind": "competitor_trend",
                                                       "trend_id": trend.id}, limit=10)
    assert len(first) == 1
    assert first[0].payload["severity"] == "high"     # "recall" ⇒ breaking
    assert first[0].payload["headline"] == trend.headline

    # a second flag supersedes the first (active flags don't pile up)
    await scout._write_flag(trend)
    active = await rb_ctx.store.query(brand="hotcars", types=[EntryType.FLAG],
                                      payload_contains={"kind": "competitor_trend",
                                                        "trend_id": trend.id}, limit=10)
    assert len(active) == 1
    assert active[0].id != first[0].id


async def test_write_flag_non_breaking_is_medium(rb_ctx):
    scout = TrendScout(rb_ctx)
    trend = await _seed_trend(rb_ctx.session, headline=f"{TOK} tesla model lineup overview")
    await scout._write_flag(trend, note="just so")
    flags = await rb_ctx.store.query(brand="hotcars", types=[EntryType.FLAG],
                                     payload_contains={"kind": "competitor_trend",
                                                       "trend_id": trend.id}, limit=5)
    assert flags[0].payload["severity"] == "medium"
    assert flags[0].payload["note"] == "just so"


async def test_write_lifecycle_flag(rb_ctx):
    scout = TrendScout(rb_ctx)
    trend = await _seed_trend(rb_ctx.session, headline=f"{TOK} tesla fading topic")
    await scout._write_lifecycle_flag(trend, {"state": "declining", "delta_pct": -42.0,
                                              "suppressed": True})
    flags = await rb_ctx.store.query(brand="hotcars", types=[EntryType.FLAG],
                                     payload_contains={"kind": "trend_declining",
                                                       "trend_id": trend.id}, limit=5)
    assert len(flags) == 1
    p = flags[0].payload
    assert p["state"] == "declining" and p["delta_pct"] == -42.0
    assert p["suppressed"] is True and p["severity"] == "medium"


# =========================================================================== #
# _update_lifecycle  (list_for_lifecycle stubbed to isolate our trend)
# =========================================================================== #
async def test_update_lifecycle_flips_to_declining_and_flags(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    today = _now().date()
    # velocity/source/signal all 0 ⇒ today's external score computes to 0.
    trend = await _seed_trend(rb_ctx.session, headline=f"{TOK} tesla fade", status="detected",
                              velocity=0.0, source_count=0, signal_count=0, state="emerging")
    for d in range(1, 7):        # six prior days of high interest
        rb_ctx.session.add(TrendActivity(trend_id=trend.id, as_of=today - timedelta(days=d),
                                         external_score=90.0))
    await rb_ctx.session.flush()
    monkeypatch.setattr(scout.trends, "list_for_lifecycle", _afn([trend]))

    flipped = await scout._update_lifecycle("hotcars")
    assert flipped == 1
    assert trend.state == "declining"
    assert trend.suppressed is True             # auto-managed (no human override)
    # today's activity sample was recorded
    rows = (await rb_ctx.session.execute(
        select(TrendActivity).where(TrendActivity.trend_id == trend.id,
                                    TrendActivity.as_of == today))).scalars().all()
    assert len(rows) == 1
    # and a declining flag was surfaced
    flags = await rb_ctx.store.query(brand="hotcars", types=[EntryType.FLAG],
                                     payload_contains={"kind": "trend_declining",
                                                       "trend_id": trend.id}, limit=5)
    assert len(flags) == 1


async def test_update_lifecycle_records_peak_at_once(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    today = _now().date()
    # today's external score computes to 60 (velocity 10); a flat, high series ⇒ peak.
    trend = await _seed_trend(rb_ctx.session, headline=f"{TOK} tesla plateau", status="detected",
                              velocity=10.0, source_count=0, signal_count=0, state="emerging")
    assert trend.peak_at is None
    for d in range(1, 7):
        rb_ctx.session.add(TrendActivity(trend_id=trend.id, as_of=today - timedelta(days=d),
                                         external_score=60.0))
    await rb_ctx.session.flush()
    monkeypatch.setattr(scout.trends, "list_for_lifecycle", _afn([trend]))

    flipped = await scout._update_lifecycle("hotcars")
    assert flipped == 0                 # peak is not a decline transition
    assert trend.state == "peak"
    assert trend.peak_at is not None    # recorded on first reaching peak


async def test_update_lifecycle_rising_does_not_flag(rb_ctx, monkeypatch):
    scout = TrendScout(rb_ctx)
    today = _now().date()
    # today's external score computes high (75) ⇒ recent-half up ⇒ rising.
    trend = await _seed_trend(rb_ctx.session, headline=f"{TOK} tesla surge", status="detected",
                              velocity=5.0, source_count=5, signal_count=10, state="emerging")
    for d in range(1, 7):
        rb_ctx.session.add(TrendActivity(trend_id=trend.id, as_of=today - timedelta(days=d),
                                         external_score=5.0))
    await rb_ctx.session.flush()
    monkeypatch.setattr(scout.trends, "list_for_lifecycle", _afn([trend]))

    flipped = await scout._update_lifecycle("hotcars")
    assert flipped == 0
    assert trend.state == "rising"
    assert trend.suppressed is False
    flags = await rb_ctx.store.query(brand="hotcars", types=[EntryType.FLAG],
                                     payload_contains={"kind": "trend_declining",
                                                       "trend_id": trend.id}, limit=5)
    assert flags == []


# =========================================================================== #
# add_manual_trend
# =========================================================================== #
async def test_add_manual_trend_rides_the_propose_path(rb_ctx, monkeypatch):
    monkeypatch.setattr(rb_ctx.settings, "trends", _cfg(auto_dossier=True))
    collect = _Rec(ret={})
    monkeypatch.setattr(scout_mod, "collect_dossier", collect)

    trend = await scout_mod.add_manual_trend(
        rb_ctx, topic=f"Tesla {TOK} Cybertruck recall", brand="hotcars", actor=USER,
        url="https://ex.example/manual")

    assert trend.origin == "manual"
    assert trend.status == TrendStatus.PROPOSED.value
    assert collect.count == 1
    assert trend.evidence[0]["origin"] == "manual" and trend.evidence[0]["source"] == USER
    assert "tesla" in (trend.entities or {}).get("oems", [])


async def test_add_manual_trend_without_dossier(rb_ctx, monkeypatch):
    monkeypatch.setattr(rb_ctx.settings, "trends", _cfg(auto_dossier=False))
    collect = _Rec(ret={})
    monkeypatch.setattr(scout_mod, "collect_dossier", collect)

    trend = await scout_mod.add_manual_trend(
        rb_ctx, topic=f"Ford {TOK} Bronco reveal", brand="topspeed", actor=USER)
    assert trend.status == TrendStatus.PROPOSED.value
    assert collect.count == 0


async def test_add_manual_trend_dossier_failure_is_swallowed(rb_ctx, monkeypatch):
    monkeypatch.setattr(rb_ctx.settings, "trends", _cfg(auto_dossier=True))
    monkeypatch.setattr(scout_mod, "collect_dossier", _Rec(exc=RuntimeError("down")))
    trend = await scout_mod.add_manual_trend(
        rb_ctx, topic=f"Honda {TOK} Civic Type R news", brand="carbuzz", actor=USER)
    assert trend.status == TrendStatus.PROPOSED.value


async def test_add_manual_trend_does_not_regress_actioned_trend(rb_ctx, monkeypatch):
    monkeypatch.setattr(rb_ctx.settings, "trends", _cfg(auto_dossier=True))
    monkeypatch.setattr(scout_mod, "collect_dossier", _Rec(ret={}))
    topic = f"Tesla {TOK} Roadster delay"
    ck = "-".join(sorted(detector.tokens(topic)))[:120] or f"manual-{topic[:40]}"
    # a trend on this exact cluster is already APPROVED — must not regress to proposed.
    rb_ctx.session.add(Trend(brand="hotcars", cluster_key=ck, headline=topic,
                             status="approved", score=60.0, last_seen_at=_now()))
    await rb_ctx.session.flush()

    trend = await scout_mod.add_manual_trend(rb_ctx, topic=topic, brand="hotcars", actor=USER)
    assert trend.status == TrendStatus.APPROVED.value   # unchanged


# =========================================================================== #
# run_trend_scan  (entry point delegates to TrendScout.scan)
# =========================================================================== #
async def test_run_trend_scan_delegates(monkeypatch):
    seen: dict = {}

    async def fake_scan(self, brand=PORTFOLIO):
        seen["brand"] = brand
        return {"enabled": True, "brand": brand, "delegated": True}

    monkeypatch.setattr(scout_mod.TrendScout, "scan", fake_scan)
    try:
        res = await scout_mod.run_trend_scan("hotcars")
    except Exception as exc:  # noqa: BLE001 — opening a RunContext needs a DB
        pytest.skip(f"no reachable Postgres: {exc}")
    assert res == {"enabled": True, "brand": "hotcars", "delegated": True}
    assert seen["brand"] == "hotcars"
