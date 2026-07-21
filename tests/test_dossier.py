"""Dossier builder tests (src/switchboard/trends/dossier.py, docs/trend-pipeline.md).

Two layers:

* Pure helpers — ``_parse_json`` (fence/JSON extraction) and ``_render_markdown``
  (dossier → markdown) — run with no DB and no mocks.
* ``collect_dossier`` / ``_synthesize`` / ``_write_claims`` are DB-backed: they
  open a real :class:`RunContext` (the ``db_ctx`` fixture skips when no Postgres
  is reachable). EVERY external boundary — the three source clients (Tavily,
  Firecrawl, Perplexity), the synthesis ``LLMClient``, and the ``ArtifactStore``
  — is monkeypatched at the ``dossier`` module namespace, so no network/API/LLM
  call and no artifact file write ever happens. Assertions reflect the ACTUAL
  source behaviour (soft-degradation, source dedupe, fact-gate claims, the
  synthesis fallback shape, cost metering).

Rows are isolated: trends carry a ``pytest-dossier-`` cluster_key marker and the
audit/claim/spend rows they produce are swept by an autouse cleanup fixture
(a no-op when no DB is reachable).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from switchboard.adapters.base import AdapterUnavailable
from switchboard.context import RunContext
from switchboard.db.enums import EntryType
from switchboard.db.models import MemoryEntry, SpendLedger, ToolCallLog, Trend
from switchboard.trends import dossier as D

# Rows created at/after this instant by the agents this module drives are ours.
_START = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Cleanup — sweep every row collect_dossier writes for a pytest trend.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
async def _scrub_dossier_rows():
    yield
    try:
        async with RunContext.open() as c:
            s = c.session
            await s.execute(delete(MemoryEntry).where(
                MemoryEntry.source_agent == "trend_scout",
                MemoryEntry.source_system == "trend_dossier",
                MemoryEntry.created_at >= _START))
            await s.execute(delete(ToolCallLog).where(
                ToolCallLog.agent == "trend_scout", ToolCallLog.created_at >= _START))
            await s.execute(delete(SpendLedger).where(
                SpendLedger.agent == "trend_scout", SpendLedger.created_at >= _START))
            await s.execute(delete(Trend).where(Trend.cluster_key.like("pytest-dossier-%")))
            await s.commit()
    except Exception:  # noqa: BLE001 — no DB / nothing to scrub
        pass


# --------------------------------------------------------------------------- #
# Fake source clients + LLM + artifact store (patched into dossier's namespace)
# --------------------------------------------------------------------------- #
class _FakeTavily:
    def __init__(self, key=None):
        pass

    async def deep_search(self, query, *, max_results=5):
        return {"answer": "ANS",
                "results": [{"title": "T1", "url": "https://a", "content": "C1"},
                            {"title": "T2", "url": "https://b", "content": "C2"}]}


class _FakeFirecrawl:
    def __init__(self, key=None):
        pass

    async def scrape(self, url):
        return {"url": url, "title": "FT", "markdown": "MD-" + url}


class _FakePerplexity:
    def __init__(self, key=None):
        pass

    async def ask(self, prompt, *, max_tokens=1024):
        return {"text": "PPX", "citations": ["https://c"], "micros": 1234}


class _Unavailable:
    """A client whose construction fails — the AdapterUnavailable soft path."""

    def __init__(self, *a, **k):
        raise AdapterUnavailable("not configured")


def _fake_llm(text, micros):
    class _LLM:
        last: dict = {}

        def __init__(self, ctx):
            self.ctx = ctx

        async def complete(self, **kw):
            _LLM.last = kw
            return SimpleNamespace(text=text, micros=micros)

    return _LLM


class _FakeLLMUnavailable:
    def __init__(self, ctx):
        pass

    async def complete(self, **kw):
        raise AdapterUnavailable("no llm")


class _FakeArtifact:
    POINTER = {"backend": "local", "key": "k.md", "uri": "file:///k.md"}

    def put_text(self, **kw):
        _FakeArtifact.last = kw
        return self.POINTER


class _BadArtifact:
    def put_text(self, **kw):
        raise RuntimeError("disk full")


def _patch_sources(monkeypatch, *, tavily=_FakeTavily, firecrawl=_FakeFirecrawl,
                   perplexity=_FakePerplexity, artifact=_FakeArtifact,
                   llm=None, llm_text="{}", llm_micros=0):
    monkeypatch.setattr(D, "TavilyClient", tavily)
    monkeypatch.setattr(D, "FirecrawlClient", firecrawl)
    monkeypatch.setattr(D, "PerplexityClient", perplexity)
    monkeypatch.setattr(D, "ArtifactStore", artifact)
    monkeypatch.setattr(D, "LLMClient", llm if llm is not None else _fake_llm(llm_text, llm_micros))


_GOOD_JSON = (
    '{"summary":"S","timeline":["2020 — launch"],'
    '"key_facts":[{"statement":"KF1","source_url":"https://k"}],'
    '"angles":[{"title":"A1","rationale":"R1","content_type":"article"}],'
    '"entities":["Toyota"],"risks":["risk1"]}'
)


async def _make_trend(ctx, **kw):
    kw.setdefault("brand", "hotcars")
    kw.setdefault("cluster_key", f"pytest-dossier-{uuid.uuid4().hex}")
    kw.setdefault("headline", "Big automotive story")
    kw.setdefault("score", 72.0)
    kw.setdefault("summary", "trend summary")
    kw.setdefault("entities", {"oems": ["Toyota"]})
    kw.setdefault("evidence", [{"source": "rival", "title": "Rival piece", "url": "https://a"}])
    trend = Trend(**kw)
    ctx.session.add(trend)
    await ctx.session.flush()
    return trend


# =========================================================================== #
# _parse_json (pure)
# =========================================================================== #
def test_parse_json_plain_object():
    assert D._parse_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_parse_json_fenced_with_json_label():
    assert D._parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_fenced_without_label():
    assert D._parse_json('```\n{"a": 2}\n```') == {"a": 2}


def test_parse_json_single_fence_marker_uses_strip_branch():
    # startswith('```') but count('```') < 2 → the ``text.strip('`')`` branch.
    assert D._parse_json('```{"a": 3}') == {"a": 3}


def test_parse_json_prose_around_object():
    assert D._parse_json('Here you go: {"a": 4} — done.') == {"a": 4}


def test_parse_json_no_braces_returns_none():
    assert D._parse_json("no json here") is None


def test_parse_json_empty_returns_none():
    assert D._parse_json("") is None
    assert D._parse_json(None) is None  # type: ignore[arg-type]


def test_parse_json_malformed_returns_none():
    assert D._parse_json("{not valid json}") is None


def test_parse_json_only_open_brace_returns_none():
    # end <= start (no closing brace) → None
    assert D._parse_json("prefix { unterminated") is None


# =========================================================================== #
# _render_markdown (pure)
# =========================================================================== #
def test_render_markdown_full():
    trend = Trend(brand="hotcars", cluster_key="k", headline="My Headline", score=88.4,
                  evidence=[{"source": "rival", "title": "Their take", "url": "https://x"}])
    dossier = {
        "summary": "The summary.",
        "timeline": ["2020 — a", "2021 — b"],
        "key_facts": [{"statement": "Fact one", "source_url": "https://f"},
                      {"statement": "Fact two"}],
        "angles": [{"title": "Angle", "rationale": "why", "content_type": "social_post"}],
        "risks": ["a risk"],
        "sources": ["https://s1", "https://s2"],
        "collected_at": "2026-07-21T00:00:00+00:00",
    }
    md = D._render_markdown(trend, dossier)
    assert md.startswith("# Trend dossier — My Headline")
    assert "score 88" in md                       # f"{score:.0f}" → 88
    assert "## Summary\nThe summary." in md
    assert "## Timeline" in md and "- 2020 — a" in md
    assert "## Key facts (pending verification)" in md
    assert "- Fact one — https://f" in md         # source_url suffix
    assert "- Fact two" in md                     # no source_url → no suffix
    assert "## Suggested angles" in md and "**Angle** (social_post): why" in md
    assert "## Risks" in md and "- a risk" in md
    assert "## Competitor coverage" in md and "[Their take](https://x)" in md
    assert "## Sources" in md and "- https://s1" in md


def test_render_markdown_minimal_only_summary():
    trend = Trend(brand="hotcars", cluster_key="k", headline="H", score=0.0, evidence=None)
    md = D._render_markdown(trend, {})
    assert "# Trend dossier — H" in md
    assert "## Summary" in md
    for absent in ("## Timeline", "## Key facts", "## Suggested angles", "## Risks",
                   "## Competitor coverage", "## Sources"):
        assert absent not in md


# =========================================================================== #
# _synthesize (DB-free: fake ctx + patched LLMClient)
# =========================================================================== #
def _synth_ctx():
    return SimpleNamespace(settings=SimpleNamespace(models=SimpleNamespace(synthesis="synth-model")))


def _synth_trend():
    return Trend(brand="hotcars", cluster_key="k", headline="Head", score=1.0,
                 summary="sum", entities={"oems": ["Toyota", "Honda"]})


async def test_synthesize_parses_and_stamps_micros(monkeypatch):
    monkeypatch.setattr(D, "LLMClient", _fake_llm(_GOOD_JSON, 7))
    out = await D._synthesize(_synth_ctx(), _synth_trend(), ["material"])
    assert out["summary"] == "S"
    assert out["key_facts"][0]["statement"] == "KF1"
    assert out["llm_micros"] == 7


async def test_synthesize_unparseable_falls_back(monkeypatch):
    monkeypatch.setattr(D, "LLMClient", _fake_llm("this is not json", 9))
    out = await D._synthesize(_synth_ctx(), _synth_trend(), ["material"])
    assert out["summary"] == "sum"                # trend.summary fallback
    assert out["key_facts"] == [] and out["timeline"] == [] and out["angles"] == []
    assert out["entities"] == ["Toyota", "Honda"]  # from entities['oems']
    assert "llm_micros" not in out                # not stamped on the fallback


async def test_synthesize_llm_unavailable_falls_back(monkeypatch):
    monkeypatch.setattr(D, "LLMClient", _FakeLLMUnavailable)
    out = await D._synthesize(_synth_ctx(), _synth_trend(), ["m"])
    assert out["summary"] == "sum" and out["key_facts"] == []


async def test_synthesize_fallback_summary_uses_headline_when_no_summary(monkeypatch):
    monkeypatch.setattr(D, "LLMClient", _fake_llm("nope", 0))
    trend = Trend(brand="hotcars", cluster_key="k", headline="The Head", score=1.0,
                  summary=None, entities=None)
    out = await D._synthesize(_synth_ctx(), trend, ["m"])
    assert out["summary"] == "The Head"           # trend.summary or trend.headline
    assert out["entities"] == []                  # (entities or {}).get('oems', [])


# =========================================================================== #
# _write_claims (DB)
# =========================================================================== #
async def test_write_claims_caps_filters_and_sets_source_urls(db_ctx):
    trend = await _make_trend(db_ctx)
    dossier = {"key_facts": [
        {"statement": "A", "source_url": "https://u"},
        {"statement": "B"},                # no source_url
        {"nope": 1},                       # dict without statement — filtered
        "not-a-dict",                      # non-dict — filtered
        {"statement": ""},                 # empty statement — filtered
        {"statement": "C"}, {"statement": "D"},
        {"statement": "E"}, {"statement": "F"},  # 6 valid total → capped at 5
    ]}
    await D._write_claims(db_ctx, trend, dossier)

    rows = await db_ctx.store.query(
        brand="hotcars", types=[EntryType.CLAIM], source_system="trend_dossier",
        payload_contains={"trend_id": trend.id}, limit=50)
    by_stmt = {r.payload["statement"]: r for r in rows}
    assert len(rows) == 5                          # _MAX_CLAIMS
    assert set(by_stmt) == {"A", "B", "C", "D", "E"}   # F dropped (past the cap)
    assert by_stmt["A"].source_urls == ["https://u"]
    assert by_stmt["B"].source_urls is None
    # provenance: CLAIM, unverified, needs_verification flag, confidence 0.5
    assert by_stmt["A"].type == EntryType.CLAIM and by_stmt["A"].verified is False
    assert by_stmt["A"].payload["needs_verification"] is True
    assert by_stmt["A"].confidence == 0.5


async def test_write_claims_no_key_facts_writes_nothing(db_ctx):
    trend = await _make_trend(db_ctx)
    await D._write_claims(db_ctx, trend, {"key_facts": []})
    rows = await db_ctx.store.query(
        brand="hotcars", types=[EntryType.CLAIM], source_system="trend_dossier",
        payload_contains={"trend_id": trend.id}, limit=50)
    assert rows == []


# =========================================================================== #
# collect_dossier (DB + every boundary mocked)
# =========================================================================== #
async def test_collect_dossier_happy_path(db_ctx, monkeypatch):
    before = await db_ctx.governor.spent_today("llm_micros")
    _patch_sources(monkeypatch, llm_text=_GOOD_JSON, llm_micros=555)
    trend = await _make_trend(db_ctx)

    dossier = await D.collect_dossier(db_ctx, trend)

    # Synthesised content survives onto the dossier.
    assert dossier["summary"] == "S"
    assert dossier["timeline"] == ["2020 — launch"]
    assert dossier["key_facts"][0]["statement"] == "KF1"
    assert dossier["angles"][0]["title"] == "A1"
    assert dossier["llm_micros"] == 555            # synthesis micros stamped
    assert "collected_at" in dossier
    # Sources are deduped (tavily a,b + firecrawl a + perplexity c), order-preserving.
    assert dossier["sources"] == ["https://a", "https://b", "https://c"]
    assert dossier["_artifact"] is True

    # Persisted onto the trend row.
    assert trend.dossier is dossier
    assert trend.dossier_ref == _FakeArtifact.POINTER

    # Key facts written to shared memory as claims (fact-gate: never verified).
    claims = await db_ctx.store.query(
        brand="hotcars", types=[EntryType.CLAIM], source_system="trend_dossier",
        payload_contains={"trend_id": trend.id}, limit=50)
    assert len(claims) == 1
    assert claims[0].payload["statement"] == "KF1"
    assert claims[0].source_urls == ["https://k"]
    assert claims[0].verified is False

    # Perplexity's reported micros were charged to the governor (trend_scout).
    after = await db_ctx.governor.spent_today("llm_micros")
    assert after - before == 1234

    # Every external pull left an audit row.
    logs = (await db_ctx.session.execute(
        select(ToolCallLog).where(ToolCallLog.agent == "trend_scout",
                                  ToolCallLog.created_at >= _START))).scalars().all()
    tools = {row.tool for row in logs}
    assert {"tavily_deep_search", "firecrawl_scrape", "perplexity_ask"} <= tools
    assert all(row.ok for row in logs)


async def test_collect_dossier_all_sources_unavailable_degrades(db_ctx, monkeypatch):
    # Every client construction fails + synthesis LLM unavailable → the softest path.
    _patch_sources(monkeypatch, tavily=_Unavailable, firecrawl=_Unavailable,
                   perplexity=_Unavailable, llm=_FakeLLMUnavailable)
    trend = await _make_trend(
        db_ctx, summary="the summary", entities={"oems": ["Toyota", "Honda"]},
        evidence=[{"source": "rival", "title": "Piece", "url": "https://ev"}])

    dossier = await D.collect_dossier(db_ctx, trend)

    # Fallback dossier shape (from _synthesize).
    assert dossier["summary"] == "the summary"
    assert dossier["key_facts"] == [] and dossier["timeline"] == []
    assert dossier["entities"] == ["Toyota", "Honda"]
    # Evidence titles are the material floor; the evidence URL becomes a source.
    assert "https://ev" in dossier["sources"]
    assert dossier["_artifact"] is True
    # No key facts → no claims written.
    claims = await db_ctx.store.query(
        brand="hotcars", types=[EntryType.CLAIM], source_system="trend_dossier",
        payload_contains={"trend_id": trend.id}, limit=50)
    assert claims == []


async def test_collect_dossier_unparseable_synthesis_uses_fallback(db_ctx, monkeypatch):
    _patch_sources(monkeypatch, llm_text="totally not json", llm_micros=42)
    trend = await _make_trend(db_ctx, summary="sfallback")

    dossier = await D.collect_dossier(db_ctx, trend)

    assert dossier["summary"] == "sfallback"       # parse failed → fallback summary
    assert "llm_micros" not in dossier
    # Sources still assembled from the (working) tavily/firecrawl/perplexity fakes.
    assert "https://a" in dossier["sources"]


async def test_collect_dossier_artifact_failure_sets_flag_false(db_ctx, monkeypatch):
    _patch_sources(monkeypatch, artifact=_BadArtifact, llm_text=_GOOD_JSON, llm_micros=0)
    trend = await _make_trend(db_ctx)

    dossier = await D.collect_dossier(db_ctx, trend)

    assert dossier["_artifact"] is False           # put_text raised → soft-fail
    assert trend.dossier_ref is None
    # The dossier itself is still complete + persisted.
    assert dossier["summary"] == "S"
    assert trend.dossier is dossier
