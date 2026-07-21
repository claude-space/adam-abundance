"""Content-generation transport tests (src/switchboard/trends/generators.py).

Coverage split by what each function actually needs:

* Pure / DB-free — ``build_brief``, ``_first_heading``, ``parse_iso``,
  ``_hcv_draft_result``, and the HTTP transports (``_gen_social_api``,
  ``_gen_newsletter_api``, ``_gen_shellagent``) + ``_gen_hc_viral``'s config
  guards. These use a lightweight fake ``ctx`` (a SimpleNamespace) and, where an
  outbound call exists, monkeypatch ``generators.post_json`` — no socket opens.
* DB-backed — ``gather_fact_context``, ``_active_style_profile``, ``_gen_llm``,
  and the ``generate`` LLM path open a real :class:`RunContext` (``db_ctx`` skips
  when no Postgres is reachable). The ``LLMClient`` is monkeypatched at the module
  namespace so the governed Anthropic call never runs and nothing is charged.

Assertions mirror the ACTUAL source: brief section caps and ordering, the
persona → style-profile → nothing priority in ``_gen_llm``, the empty-draft
error, transport error/pending handling, and output parsing.

Isolation: DB rows use a non-portfolio ``zz_pytest_gen`` brand (style/persona) or
a ``pytest-gen-`` trend cluster_key marker; memory writes carry dedicated
``source_system`` markers. An autouse fixture sweeps them (no-op without a DB).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from switchboard.adapters.base import AdapterUnavailable
from switchboard.context import RunContext
from switchboard.db.enums import EntryType
from switchboard.db.models import (
    ContentJob,
    ContentPipeline,
    MemoryEntry,
    Trend,
    WriterPersona,
    WriterStyleProfile,
)
from switchboard.interfaces import EntryDraft
from switchboard.trends import generators as G

GEN_BRAND = "zz_pytest_gen"        # non-portfolio; isolates brand-scoped lookups
CLAIM_SS = "pytest_gen_claim"      # source_system markers for cheap cleanup
FACT_SS = "pytest_gen_fact"


@pytest.fixture(autouse=True)
async def _scrub_generator_rows():
    yield
    try:
        async with RunContext.open() as c:
            s = c.session
            await s.execute(delete(MemoryEntry).where(
                MemoryEntry.source_system.in_([CLAIM_SS, FACT_SS])))
            await s.execute(delete(WriterStyleProfile).where(WriterStyleProfile.brand == GEN_BRAND))
            await s.execute(delete(WriterPersona).where(WriterPersona.brand == GEN_BRAND))
            await s.execute(delete(Trend).where(Trend.cluster_key.like("pytest-gen-%")))
            await s.commit()
    except Exception:  # noqa: BLE001 — no DB / nothing to scrub
        pass


# --------------------------------------------------------------------------- #
# Transient ORM object builders (never added to a session unless a test does)
# --------------------------------------------------------------------------- #
def _trend(**kw):
    kw.setdefault("brand", GEN_BRAND)
    kw.setdefault("cluster_key", "pytest-gen-transient")
    kw.setdefault("headline", "Big EV news")
    kw.setdefault("score", 80.0)
    kw.setdefault("summary", "trend summary")
    kw.setdefault("entities", {})
    kw.setdefault("evidence", [])
    kw.setdefault("dossier", None)
    return Trend(**kw)


def _pipeline(**kw):
    kw.setdefault("brand", GEN_BRAND)
    kw.setdefault("instructions", None)
    return ContentPipeline(**kw)


def _job(**kw):
    kw.setdefault("content_type", "article")
    kw.setdefault("transport", "llm")
    kw.setdefault("instructions", None)
    return ContentJob(**kw)


# --------------------------------------------------------------------------- #
# Fakes: LLMClient (module namespace) + post_json
# --------------------------------------------------------------------------- #
class _FakeLLM:
    text: str = ""
    micros: int = 0
    calls: list = []

    def __init__(self, ctx):
        self.ctx = ctx

    async def complete(self, *, system, prompt, model=None, max_tokens=1024,
                       tools=None, agent="system"):
        _FakeLLM.calls.append({"system": system, "prompt": prompt, "model": model,
                               "max_tokens": max_tokens, "agent": agent})
        return SimpleNamespace(text=_FakeLLM.text, micros=_FakeLLM.micros)


def _use_llm(monkeypatch, text, micros=0):
    _FakeLLM.text, _FakeLLM.micros, _FakeLLM.calls = text, micros, []
    monkeypatch.setattr(G, "LLMClient", _FakeLLM)


def _use_post_json(monkeypatch, handler):
    """Patch generators.post_json; handler(base, path, json, headers) -> data."""
    calls: list = []

    async def fake(base, path, *, json=None, headers=None, params=None, timeout=60.0):
        calls.append({"base": base, "path": path, "json": json, "headers": headers})
        return handler(base, path, json, headers)

    monkeypatch.setattr(G, "post_json", fake)
    return calls


# =========================================================================== #
# build_brief (pure)
# =========================================================================== #
def test_build_brief_full_sections_and_caps():
    trend = _trend(
        headline="H",
        dossier={"summary": "DS",
                 "timeline": [f"t{i}" for i in range(10)],
                 "angles": [{"title": f"A{i}", "rationale": "R"} for i in range(7)]},
        evidence=[{"source": "s", "title": "ti", "url": f"https://u{i}"} for i in range(12)])
    pipe = _pipeline(brand="hotcars", instructions="pipe-instr")
    job = _job(content_type="article", instructions="job-instr")
    brief = G.build_brief(trend, pipe, job,
                          [f"vf{i}" for i in range(10)], [f"pc{i}" for i in range(10)])

    assert "TREND: H" in brief
    assert "BRAND: hotcars" in brief
    assert "CONTENT TYPE: article" in brief
    assert "SUMMARY:\nDS" in brief                       # dossier summary preferred
    # timeline capped at 8
    assert "- t7" in brief and "- t8" not in brief
    # verified facts + claims sections, each capped at 8
    assert "VERIFIED FACTS" in brief and "- vf7" in brief and "- vf8" not in brief
    assert "UNVERIFIED CLAIMS" in brief and "- pc7" in brief and "- pc8" not in brief
    # angles capped at 5
    assert "SUGGESTED ANGLES" in brief and "A4:" in brief and "A5:" not in brief
    # evidence capped at 10 (u0..u9 kept, u10/u11 dropped)
    assert "— https://u9" in brief and "https://u10" not in brief
    # editor instructions are the two joined with a space
    assert "EDITOR INSTRUCTIONS (must follow): pipe-instr job-instr" in brief
    # the article ask lands last
    assert "publication-ready news article draft" in brief


def test_build_brief_no_dossier_uses_trend_summary_and_omits_optional_sections():
    trend = _trend(dossier=None, summary="TS", evidence=[])
    brief = G.build_brief(trend, _pipeline(), _job(content_type="social_post"), [], [])
    assert "SUMMARY:\nTS" in brief
    for absent in ("VERIFIED FACTS", "UNVERIFIED CLAIMS", "SUGGESTED ANGLES",
                   "COMPETITOR COVERAGE", "EDITOR INSTRUCTIONS", "TIMELINE:"):
        assert absent not in brief
    assert "Write social media post options" in brief   # social_post ask


def test_build_brief_no_summary_anywhere_uses_placeholder():
    trend = _trend(dossier={}, summary=None, evidence=[])
    brief = G.build_brief(trend, _pipeline(), _job(), [], [])
    assert "(no dossier — rely on the evidence below)" in brief


def test_build_brief_unknown_content_type_falls_back_to_article_ask():
    brief = G.build_brief(_trend(), _pipeline(), _job(content_type="mystery"), [], [])
    assert "publication-ready news article draft" in brief


def test_build_brief_instructions_pipeline_only():
    brief = G.build_brief(_trend(), _pipeline(instructions="only-pipe"),
                          _job(instructions=None), [], [])
    assert "EDITOR INSTRUCTIONS (must follow): only-pipe" in brief


# =========================================================================== #
# _first_heading / parse_iso (pure)
# =========================================================================== #
def test_first_heading_hash_line():
    assert G._first_heading("# Title Here\nbody") == "Title Here"
    assert G._first_heading("## Sub Heading\nx") == "Sub Heading"


def test_first_heading_first_nonempty_truncated_to_120():
    out = G._first_heading("\n\n" + "x" * 200)
    assert len(out) == 120 and out == "x" * 120


def test_first_heading_empty_string():
    assert G._first_heading("   \n  \n") == ""


def test_parse_iso_valid():
    dt = G.parse_iso("2026-07-21T10:30:00+00:00")
    assert dt is not None and dt.year == 2026 and dt.hour == 10


def test_parse_iso_none_and_invalid():
    assert G.parse_iso(None) is None
    assert G.parse_iso("not-a-date") is None


# =========================================================================== #
# _hcv_draft_result (pure)
# =========================================================================== #
def test_hcv_draft_result_full():
    draft = {"title": "DT", "seo_title": "ST", "slug": "sl", "html": "<p>b</p>",
             "sources_list": ["s1", "s2"], "word_count": 123, "excerpt": "ex"}
    res = G._hcv_draft_result(draft, _trend(headline="H"), {"topic_id": 9})
    assert res.ok is True
    assert res.preview_markdown.startswith("# DT")
    assert "SEO title: ST · slug: sl" in res.preview_markdown
    assert "<p>b</p>" in res.preview_markdown
    assert "## Sources" in res.preview_markdown and "- s1" in res.preview_markdown
    assert res.preview_meta == {"title": "DT", "generator": "hc_viral_hits",
                                "word_count": 123, "seo_title": "ST", "excerpt": "ex"}
    assert res.external_ref == {"topic_id": 9}


def test_hcv_draft_result_defaults_title_from_trend_and_omits_sources():
    res = G._hcv_draft_result({}, _trend(headline="Fallback H"), {})
    assert res.preview_markdown.startswith("# Fallback H")   # title defaults to headline
    assert "## Sources" not in res.preview_markdown          # no sources_list
    assert res.preview_meta["title"] == ""                   # meta title from draft.get('title','')


# =========================================================================== #
# _gen_social_api (post_json mocked, fake ctx)
# =========================================================================== #
def _ctx_social(url="http://social"):
    return SimpleNamespace(settings=SimpleNamespace(endpoints={"social": url}))


async def test_gen_social_happy(monkeypatch):
    calls = _use_post_json(monkeypatch, lambda *_: {
        "captions": {"Instagram": ["cap a", "cap b"], "X": ["cap c"]},
        "excerpts": ["excerpt one"]})
    trend = _trend(headline="H", dossier={"summary": "DS"}, evidence=[{"url": "https://u"}])
    res = await G._gen_social_api(_ctx_social(), _pipeline(), trend)

    assert res.ok is True
    md = res.preview_markdown
    assert "# Social posts — H" in md
    assert "## Instagram" in md and "1. cap a" in md and "2. cap b" in md
    assert "## X" in md and "1. cap c" in md
    assert "## On-image excerpts (verbatim)" in md and "- excerpt one" in md
    assert res.preview_meta["generator"] == "social_api"
    assert res.preview_meta["raw"]["captions"]["X"] == ["cap c"]
    # request body: title=headline, bodyText=dossier.summary, url from evidence
    assert calls[0]["path"] == "/api/generate"
    assert calls[0]["json"] == {"title": "H", "bodyText": "DS", "url": "https://u"}


async def test_gen_social_body_text_falls_back_to_headline(monkeypatch):
    calls = _use_post_json(monkeypatch, lambda *_: {"captions": {"X": ["c"]}})
    trend = _trend(headline="Only Headline", dossier=None, summary=None, evidence=[])
    await G._gen_social_api(_ctx_social(), _pipeline(), trend)
    assert calls[0]["json"] == {"title": "Only Headline", "bodyText": "Only Headline", "url": None}


async def test_gen_social_non_list_caption_wrapped(monkeypatch):
    _use_post_json(monkeypatch, lambda *_: {"captions": {"Facebook": "single caption"}})
    res = await G._gen_social_api(_ctx_social(), _pipeline(), _trend())
    assert "## Facebook" in res.preview_markdown and "1. single caption" in res.preview_markdown


async def test_gen_social_no_captions_returns_error(monkeypatch):
    _use_post_json(monkeypatch, lambda *_: {"captions": {}})
    res = await G._gen_social_api(_ctx_social(), _pipeline(), _trend())
    assert res.ok is False and res.error == "social API returned no captions"


# =========================================================================== #
# _gen_newsletter_api (post_json mocked, fake ctx)
# =========================================================================== #
def _ctx_news(url="http://news"):
    return SimpleNamespace(settings=SimpleNamespace(endpoints={"newsletter": url}))


async def test_gen_newsletter_happy(monkeypatch):
    calls = _use_post_json(monkeypatch, lambda *_: {
        "subject": "S", "lead": "L", "count": 3, "ratio": 1.5,
        "warnings": ["w"], "obj": {"x": 1}})
    trend = _trend(headline="H", evidence=[{"url": "https://u"}])
    res = await G._gen_newsletter_api(_ctx_news(), trend)

    assert res.ok is True
    md = res.preview_markdown
    assert md.startswith("# Newsletter blurb — H")
    # scalar fields rendered; 'warnings' excluded by name, dict 'obj' excluded by type
    assert "- **subject**: S" in md and "- **lead**: L" in md
    assert "- **count**: 3" in md and "- **ratio**: 1.5" in md
    assert "warnings" not in md and "obj" not in md
    assert res.preview_meta["generator"] == "newsletter_api"
    assert res.preview_meta["raw"]["obj"] == {"x": 1}
    assert calls[0]["path"] == "/api/article/process"
    assert calls[0]["json"] == {"url": "https://u", "role": "brief", "title": "H"}


async def test_gen_newsletter_no_evidence_url_raises(monkeypatch):
    _use_post_json(monkeypatch, lambda *_: {})   # never reached
    with pytest.raises(AdapterUnavailable):
        await G._gen_newsletter_api(_ctx_news(), _trend(evidence=[]))


# =========================================================================== #
# _gen_shellagent (post_json mocked, fake creds)
# =========================================================================== #
def _ctx_agent(url, token):
    return SimpleNamespace(creds=SimpleNamespace(trend_agent=lambda ct: (url, token)))


async def test_gen_shellagent_not_configured_raises():
    with pytest.raises(AdapterUnavailable):
        await G._gen_shellagent(_ctx_agent(None, None), _job(content_type="article"), "brief")


async def test_gen_shellagent_output_happy(monkeypatch):
    calls = _use_post_json(monkeypatch, lambda *_: {"output": "# Draft\nbody"})
    res = await G._gen_shellagent(_ctx_agent("http://agent", "tok"),
                                  _job(content_type="article"), "the brief")
    assert res.ok is True
    assert res.preview_markdown == "# Draft\nbody"
    assert res.preview_meta == {"title": "Draft", "generator": "shellagent_run",
                                "agent_url": "http://agent"}
    assert calls[0]["base"] == "http://agent" and calls[0]["path"] == "/run"
    assert calls[0]["json"] == {"input": "the brief"}
    assert calls[0]["headers"] == {"Authorization": "Bearer tok"}


async def test_gen_shellagent_title_fallback_when_no_heading(monkeypatch):
    _use_post_json(monkeypatch, lambda *_: {"output": "\n  \n"})
    res = await G._gen_shellagent(_ctx_agent("http://a", "t"),
                                  _job(content_type="video_script"), "b")
    assert res.ok is True and res.preview_meta["title"] == "video_script draft"


async def test_gen_shellagent_no_output_returns_error(monkeypatch):
    _use_post_json(monkeypatch, lambda *_: {"error": "boom"})
    res = await G._gen_shellagent(_ctx_agent("http://a", "t"), _job(), "b")
    assert res.ok is False and "boom" in res.error


async def test_gen_shellagent_no_token_sends_no_auth_header(monkeypatch):
    calls = _use_post_json(monkeypatch, lambda *_: {"output": "x"})
    await G._gen_shellagent(_ctx_agent("http://a", None), _job(), "b")
    assert calls[0]["headers"] == {}


# =========================================================================== #
# _gen_hc_viral — config guards (no network)
# =========================================================================== #
def _ctx_hcv(endpoints, resolve_map):
    return SimpleNamespace(
        settings=SimpleNamespace(endpoints=endpoints),
        creds=SimpleNamespace(resolve=lambda k, secret=True: resolve_map.get(k)))


async def test_gen_hc_viral_no_base_or_key_raises():
    ctx = _ctx_hcv({"hc_viral_hits": ""}, {})
    with pytest.raises(AdapterUnavailable):
        await G._gen_hc_viral(ctx, _job(external_ref=None), _pipeline(), _trend())


async def test_gen_hc_viral_missing_login_creds_raises():
    ctx = _ctx_hcv({"hc_viral_hits": "http://hc"}, {"HC_VIRAL_HITS_API_KEY": "key"})
    with pytest.raises(AdapterUnavailable):
        await G._gen_hc_viral(ctx, _job(external_ref=None), _pipeline(),
                              _trend(evidence=[{"url": "https://u"}]))


async def test_gen_hc_viral_no_evidence_url_raises():
    ctx = _ctx_hcv({"hc_viral_hits": "http://hc"},
                   {"HC_VIRAL_HITS_API_KEY": "key", "HC_VIRAL_HITS_LOGIN_EMAIL": "e",
                    "HC_VIRAL_HITS_LOGIN_PASSWORD": "p"})
    with pytest.raises(AdapterUnavailable):
        await G._gen_hc_viral(ctx, _job(external_ref=None), _pipeline(), _trend(evidence=[]))


# =========================================================================== #
# generate — dispatch + error handling
# =========================================================================== #
async def test_generate_social_api_routes(monkeypatch):
    _use_post_json(monkeypatch, lambda *_: {"captions": {"X": ["a"]}})
    ctx = _ctx_social()
    res = await G.generate(ctx, _job(transport="social_api"), _pipeline(),
                           _trend(evidence=[{"url": "https://u"}]))
    assert res.ok is True and "## X" in res.preview_markdown


async def test_generate_adapter_unavailable_wrapped(monkeypatch):
    def boom(*a, **k):
        raise AdapterUnavailable("down")
    monkeypatch.setattr(G, "_gen_social_api", boom)
    res = await G.generate(SimpleNamespace(settings=SimpleNamespace(endpoints={})),
                           _job(transport="social_api"), _pipeline(), _trend())
    assert res.ok is False and res.error == "social_api unavailable: down"


async def test_generate_generic_error_preserves_external_ref(monkeypatch):
    def boom(*a, **k):
        raise ValueError("kaboom")
    monkeypatch.setattr(G, "_gen_social_api", boom)
    job = _job(transport="social_api", external_ref={"topic_id": 7})
    res = await G.generate(SimpleNamespace(settings=SimpleNamespace(endpoints={})),
                           job, _pipeline(), _trend())
    assert res.ok is False
    assert res.error == "social_api failed: kaboom"
    assert res.external_ref == {"topic_id": 7}


# =========================================================================== #
# gather_fact_context (DB) — verified/pending partition
# =========================================================================== #
async def test_gather_fact_context_partitions_verified_and_pending(db_ctx):
    u = uuid.uuid4().hex[:8]
    s1, s2, s3 = f"S1-{u}", f"S2-{u}", f"S3-{u}"
    trend = Trend(brand="hotcars", cluster_key=f"pytest-gen-{u}", headline="H",
                  score=10.0, dossier={"key_facts": [{"statement": s1}]})
    db_ctx.session.add(trend)
    await db_ctx.session.flush()

    for s in (s1, s2):   # unverified claims tied to the trend
        await db_ctx.store.write(EntryDraft(
            type=EntryType.CLAIM, brand="hotcars", source_agent="trend_scout",
            source_system=CLAIM_SS,
            payload={"kind": "trend_key_fact", "trend_id": trend.id,
                     "statement": s, "needs_verification": True}, confidence=0.5))
    # verified facts: s1 is referenced by the dossier/claims; s3 is not.
    for s in (s1, s3):
        await db_ctx.store.write(EntryDraft(
            type=EntryType.FACT, brand="hotcars", source_agent="research",
            source_system=FACT_SS,
            payload={"kind": "verified_fact", "statement": s}, verified=True),
            fact_gate_ok=True)

    verified, pending = await G.gather_fact_context(db_ctx, trend)

    assert verified == [s1]              # only the fact whose statement is referenced
    assert set(pending) == {s2}          # s1 dropped from pending (now verified)


async def test_gather_fact_context_empty_when_nothing_written(db_ctx):
    u = uuid.uuid4().hex[:8]
    trend = Trend(brand="hotcars", cluster_key=f"pytest-gen-{u}", headline="H",
                  score=1.0, dossier=None)
    db_ctx.session.add(trend)
    await db_ctx.session.flush()
    verified, pending = await G.gather_fact_context(db_ctx, trend)
    assert verified == [] and pending == []


# =========================================================================== #
# _active_style_profile (DB)
# =========================================================================== #
async def test_active_style_profile_none_for_unknown_brand(db_ctx):
    assert await G._active_style_profile(db_ctx, GEN_BRAND) is None


async def test_active_style_profile_returns_highest_active_version(db_ctx):
    db_ctx.session.add(WriterStyleProfile(brand=GEN_BRAND, version=1, source_authors=["A"],
                                          features={"voice": "v1"}, active=True))
    db_ctx.session.add(WriterStyleProfile(brand=GEN_BRAND, version=2, source_authors=["A", "B"],
                                          features={"voice": "v2"}, active=True))
    await db_ctx.session.flush()
    prof = await G._active_style_profile(db_ctx, GEN_BRAND)
    assert prof is not None and prof.version == 2 and prof.features["voice"] == "v2"


# =========================================================================== #
# _gen_llm (DB + mocked LLMClient)
# =========================================================================== #
async def test_gen_llm_no_style_no_persona(db_ctx, monkeypatch):
    draft = "# Draft Title\n\nSome body words here."
    _use_llm(monkeypatch, text=draft, micros=321)
    job = _job(content_type="article", persona_id=None)
    job.pipeline = _pipeline(brand=GEN_BRAND)

    res = await G._gen_llm(db_ctx, job, "the brief")

    assert res.ok is True
    assert res.preview_markdown == draft
    assert res.cost_micros == 321
    meta = res.preview_meta
    assert meta["title"] == "Draft Title"
    assert meta["word_count"] == len(draft.split())
    assert meta["generator"] == "switchboard-llm"
    assert meta["model"] == db_ctx.settings.models.default
    assert meta["used_style_profile"] is False
    assert "persona_id" not in meta and "style_profile_id" not in meta

    call = _FakeLLM.calls[-1]
    assert call["agent"] == "trend_pipeline"
    assert call["max_tokens"] == 3000                 # _LLM_MAX_TOKENS['article']
    assert call["model"] == db_ctx.settings.models.default
    assert GEN_BRAND in call["system"]                # system prompt is brand-formatted
    assert "HOUSE STYLE GUIDE" not in call["system"]  # no guide appended


async def test_gen_llm_unknown_content_type_default_max_tokens(db_ctx, monkeypatch):
    _use_llm(monkeypatch, text="# T\nbody", micros=0)
    job = _job(content_type="mystery", persona_id=None)
    job.pipeline = _pipeline(brand=GEN_BRAND)
    await G._gen_llm(db_ctx, job, "b")
    assert _FakeLLM.calls[-1]["max_tokens"] == 2000   # default when type not in the map


async def test_gen_llm_empty_draft_returns_error(db_ctx, monkeypatch):
    _use_llm(monkeypatch, text="   \n  ", micros=0)
    job = _job(persona_id=None)
    job.pipeline = _pipeline(brand=GEN_BRAND)
    res = await G._gen_llm(db_ctx, job, "b")
    assert res.ok is False and res.error == "LLM returned an empty draft"


async def test_gen_llm_injects_active_style_profile(db_ctx, monkeypatch):
    prof = WriterStyleProfile(brand=GEN_BRAND, version=3, source_authors=["A"],
                              features={"voice": "wry", "dos": ["lead hard"]}, active=True)
    db_ctx.session.add(prof)
    await db_ctx.session.flush()
    _use_llm(monkeypatch, text="# T\nbody", micros=5)
    job = _job(persona_id=None)
    job.pipeline = _pipeline(brand=GEN_BRAND)

    res = await G._gen_llm(db_ctx, job, "b")

    meta = res.preview_meta
    assert meta["used_style_profile"] is True
    assert meta["style_profile_id"] == prof.id
    assert meta["style_profile_version"] == 3
    assert "persona_id" not in meta
    call = _FakeLLM.calls[-1]
    assert "HOUSE STYLE GUIDE" in call["system"] and "wry" in call["system"]


async def test_gen_llm_persona_overrides_profile(db_ctx, monkeypatch):
    # An active profile exists, but a chosen persona takes priority.
    db_ctx.session.add(WriterStyleProfile(brand=GEN_BRAND, version=1, source_authors=["A"],
                                          features={"voice": "profile-voice"}, active=True))
    persona = WriterPersona(brand=GEN_BRAND, kind="house", name=f"Snappy-{uuid.uuid4().hex[:6]}",
                            features={"voice": "snappy"}, style_brief="be punchy", enabled=True)
    db_ctx.session.add(persona)
    await db_ctx.session.flush()
    _use_llm(monkeypatch, text="# T\nb", micros=1)
    job = _job(persona_id=persona.id)
    job.pipeline = _pipeline(brand=GEN_BRAND)

    res = await G._gen_llm(db_ctx, job, "b")

    meta = res.preview_meta
    assert meta["persona_id"] == persona.id
    assert meta["persona_name"] == persona.name
    assert meta["persona_kind"] == "house"
    assert meta["used_style_profile"] is True
    assert "style_profile_id" not in meta            # persona short-circuits the profile
    call = _FakeLLM.calls[-1]
    assert "snappy" in call["system"] or "be punchy" in call["system"]


async def test_generate_llm_end_to_end(db_ctx, monkeypatch):
    _use_llm(monkeypatch, text="# Gen\nbody words", micros=99)
    u = uuid.uuid4().hex[:8]
    trend = Trend(brand=GEN_BRAND, cluster_key=f"pytest-gen-{u}", headline="H",
                  score=5.0, dossier={"summary": "DS"}, evidence=[])
    db_ctx.session.add(trend)
    await db_ctx.session.flush()
    job = _job(transport="llm", persona_id=None)
    job.pipeline = _pipeline(brand=GEN_BRAND)

    res = await G.generate(db_ctx, job, job.pipeline, trend)

    assert res.ok is True
    assert res.preview_markdown.startswith("# Gen")
    assert res.cost_micros == 99
    # generate routed through gather_fact_context + build_brief; the brief reached the LLM.
    assert "TREND: H" in _FakeLLM.calls[-1]["prompt"]
