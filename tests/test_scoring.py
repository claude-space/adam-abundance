"""Artifact quality scoring (§16) — pure-logic unit tests.

Covers ``_clamp``, ``_parse_json``, ``fact_gate`` (all pure) and ``score_draft``
(async; the LLM call is stubbed via monkeypatching ``scoring.LLMClient`` so no
network/DB is touched). Assertions reflect the module's ACTUAL behaviour,
including its banker's-rounding and the uncaught ``OverflowError`` on infinities.
"""
import json
from types import SimpleNamespace

import pytest

from switchboard.trends import scoring
from switchboard.trends.scoring import WEIGHTS


# -- fixtures / helpers --------------------------------------------------------

def _ctx():
    """Minimal RunContext stand-in: score_draft only reads settings.models.default."""
    return SimpleNamespace(settings=SimpleNamespace(models=SimpleNamespace(default="model-x")))


class _Result:
    """Stand-in for adapters.clients.llm.LLMResult (only .text / .micros used)."""
    def __init__(self, text="", micros=0):
        self.text = text
        self.micros = micros


def _install_llm(monkeypatch, *, text=None, micros=0, exc=None, result=..., calls=None):
    """Swap scoring.LLMClient for a fake whose .complete() is controllable."""
    class _FakeLLM:
        def __init__(self, ctx):
            self.ctx = ctx

        async def complete(self, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            if exc is not None:
                raise exc
            if result is not ...:
                return result
            return _Result(text=text or "", micros=micros)

    monkeypatch.setattr(scoring, "LLMClient", _FakeLLM)


def _factors(factuality=90, editorial_fit=80, freshness=70, seo_ceiling=60,
             brand_voice=50, **extra):
    d = {"factuality": factuality, "editorial_fit": editorial_fit,
         "freshness": freshness, "seo_ceiling": seo_ceiling, "brand_voice": brand_voice}
    d.update(extra)
    return json.dumps(d)


# -- WEIGHTS contract ----------------------------------------------------------

def test_weights_match_spa_contract():
    # MUST mirror the SPA's 30/25/20/15/10 display and sum to exactly 1.0.
    assert WEIGHTS == {"factuality": 0.30, "editorial_fit": 0.25, "freshness": 0.20,
                       "seo_ceiling": 0.15, "brand_voice": 0.10}
    assert round(sum(WEIGHTS.values()), 10) == 1.0


# -- _clamp --------------------------------------------------------------------

def test_clamp_normal_values():
    assert scoring._clamp(50) == 50
    assert scoring._clamp(50.4) == 50
    assert scoring._clamp(50.6) == 51
    assert scoring._clamp("75") == 75          # numeric string coerces via float()
    assert scoring._clamp(0) == 0
    assert scoring._clamp(100.0) == 100
    assert scoring._clamp(True) == 1           # bool is an int subclass


def test_clamp_bounds_are_0_100():
    assert scoring._clamp(-10) == 0
    assert scoring._clamp(-0.4) == 0
    assert scoring._clamp(150) == 100
    assert scoring._clamp(10_000) == 100


def test_clamp_uses_bankers_rounding():
    # int(round(float(v))) — round() is banker's rounding, ties to even.
    assert scoring._clamp(2.5) == 2
    assert scoring._clamp(0.5) == 0
    assert scoring._clamp(3.5) == 4


def test_clamp_invalid_returns_none():
    assert scoring._clamp("abc") is None
    assert scoring._clamp(None) is None        # float(None) -> TypeError
    assert scoring._clamp("   ") is None
    assert scoring._clamp([1]) is None         # float([1]) -> TypeError
    assert scoring._clamp({}) is None
    assert scoring._clamp(float("nan")) is None  # round(nan) -> ValueError, caught


def test_clamp_infinity_raises_overflow():
    # OverflowError is NOT in the caught (TypeError, ValueError) tuple, so it
    # propagates. json.loads("1e400") yields inf, so a giant factor crashes.
    with pytest.raises(OverflowError):
        scoring._clamp(float("inf"))


# -- _parse_json ---------------------------------------------------------------

def test_parse_json_plain_object():
    assert scoring._parse_json('{"a": 1, "b": 2}') == {"a": 1, "b": 2}


def test_parse_json_extracts_object_from_prose():
    assert scoring._parse_json('Sure! {"a": 1} done.') == {"a": 1}


def test_parse_json_nested_object():
    assert scoring._parse_json('{"a": {"b": 2}, "c": [1, 2]}') == {"a": {"b": 2}, "c": [1, 2]}


def test_parse_json_none_and_empty_and_no_braces():
    assert scoring._parse_json(None) is None
    assert scoring._parse_json("") is None
    assert scoring._parse_json("no json here") is None
    assert scoring._parse_json("[1, 2, 3]") is None   # regex requires braces


def test_parse_json_greedy_span_is_invalid():
    # r"\{.*\}" is greedy+DOTALL: matches first '{' .. last '}', which spans two
    # objects here and is not valid JSON -> None.
    assert scoring._parse_json('{"a":1} middle {"b":2}') is None


def test_parse_json_malformed_returns_none():
    assert scoring._parse_json("{bad}") is None
    assert scoring._parse_json('{"a": }') is None


# -- score_draft: short-circuits & failure paths -------------------------------

async def test_score_draft_empty_body_skips_llm(monkeypatch):
    calls = []
    _install_llm(monkeypatch, text=_factors(), calls=calls)
    for body in ("", "   ", "\n\t ", None):
        assert await scoring.score_draft(_ctx(), "hotcars", "article", body) is None
    assert calls == []                         # LLM never constructed/called


async def test_score_draft_llm_exception_returns_none(monkeypatch):
    _install_llm(monkeypatch, exc=RuntimeError("boom"))
    assert await scoring.score_draft(_ctx(), "hotcars", "article", "real body") is None


async def test_score_draft_unparseable_text_returns_none(monkeypatch):
    _install_llm(monkeypatch, text="the model refused to emit json")
    assert await scoring.score_draft(_ctx(), "hotcars", "article", "body") is None


async def test_score_draft_empty_object_returns_none(monkeypatch):
    # '{}' parses to a falsy dict -> `if not data` short-circuits.
    _install_llm(monkeypatch, text="{}")
    assert await scoring.score_draft(_ctx(), "hotcars", "article", "body") is None


async def test_score_draft_missing_factor_returns_none(monkeypatch):
    payload = json.dumps({"factuality": 90, "editorial_fit": 80,
                          "freshness": 70, "seo_ceiling": 60})  # no brand_voice
    _install_llm(monkeypatch, text=payload)
    assert await scoring.score_draft(_ctx(), "hotcars", "article", "body") is None


async def test_score_draft_invalid_factor_returns_none(monkeypatch):
    _install_llm(monkeypatch, text=_factors(factuality="not-a-number"))
    assert await scoring.score_draft(_ctx(), "hotcars", "article", "body") is None


async def test_score_draft_no_text_attr_returns_none(monkeypatch):
    # getattr(result, "text", "") -> "" -> parse None -> None
    _install_llm(monkeypatch, result=SimpleNamespace(micros=5))
    assert await scoring.score_draft(_ctx(), "hotcars", "article", "body") is None


# -- score_draft: happy paths --------------------------------------------------

async def test_score_draft_happy_path(monkeypatch):
    _install_llm(monkeypatch, text=_factors(note="clean and sourced"), micros=4200)
    out = await scoring.score_draft(_ctx(), "hotcars", "article", "a real draft body")
    # 90*.30 + 80*.25 + 70*.20 + 60*.15 + 50*.10 = 75
    assert out["score"] == 75
    assert out["breakdown"] == {"factuality": 90, "editorial_fit": 80, "freshness": 70,
                                "seo_ceiling": 60, "brand_voice": 50}
    assert out["note"] == "clean and sourced"
    assert out["micros"] == 4200


async def test_score_draft_all_100_and_all_0(monkeypatch):
    _install_llm(monkeypatch, text=_factors(factuality=100, editorial_fit=100, freshness=100,
                                            seo_ceiling=100, brand_voice=100))
    hi = await scoring.score_draft(_ctx(), "hotcars", "article", "body")
    assert hi["score"] == 100

    _install_llm(monkeypatch, text=_factors(factuality=0, editorial_fit=0, freshness=0,
                                            seo_ceiling=0, brand_voice=0))
    lo = await scoring.score_draft(_ctx(), "hotcars", "article", "body")
    assert lo["score"] == 0


async def test_score_draft_clamps_breakdown_then_scores(monkeypatch):
    # out-of-range + string factors get clamped BEFORE the weighted sum.
    _install_llm(monkeypatch, text=_factors(factuality=150, editorial_fit=80, freshness=70,
                                            seo_ceiling=-20, brand_voice="50"))
    out = await scoring.score_draft(_ctx(), "hotcars", "article", "body")
    assert out["breakdown"] == {"factuality": 100, "editorial_fit": 80, "freshness": 70,
                                "seo_ceiling": 0, "brand_voice": 50}
    # 100*.30 + 80*.25 + 70*.20 + 0*.15 + 50*.10 = 69
    assert out["score"] == 69


async def test_score_draft_note_defaults_when_absent(monkeypatch):
    _install_llm(monkeypatch, text=_factors())              # no note key
    out = await scoring.score_draft(_ctx(), "hotcars", "article", "body")
    assert out["note"] == ""


async def test_score_draft_note_truncated_to_200(monkeypatch):
    _install_llm(monkeypatch, text=_factors(note="x" * 250))
    out = await scoring.score_draft(_ctx(), "hotcars", "article", "body")
    assert out["note"] == "x" * 200


async def test_score_draft_nonstring_note_coerced(monkeypatch):
    _install_llm(monkeypatch, text=_factors(note=123))
    out = await scoring.score_draft(_ctx(), "hotcars", "article", "body")
    assert out["note"] == "123"                             # str(123 or "")


async def test_score_draft_micros_defaults_zero(monkeypatch):
    # result exposes no micros attr -> getattr default 0
    _install_llm(monkeypatch, result=SimpleNamespace(text=_factors()))
    out = await scoring.score_draft(_ctx(), "hotcars", "article", "body")
    assert out["micros"] == 0

    # explicit falsy micros -> `or 0`
    _install_llm(monkeypatch, text=_factors(), micros=0)
    out2 = await scoring.score_draft(_ctx(), "hotcars", "article", "body")
    assert out2["micros"] == 0


async def test_score_draft_builds_prompt_and_call_args(monkeypatch):
    calls = []
    _install_llm(monkeypatch, text=_factors(), calls=calls)
    long_body = "a" * 7000
    await scoring.score_draft(_ctx(), "TopSpeed", "social_post", long_body)
    assert len(calls) == 1
    kw = calls[0]
    assert "TopSpeed" in kw["system"]                       # _SYSTEM.format(brand=...)
    assert "CONTENT TYPE: social_post" in kw["prompt"]
    assert ("a" * 6000) in kw["prompt"]                     # body sliced to 6000
    assert ("a" * 6001) not in kw["prompt"]
    assert kw["max_tokens"] == 300
    assert kw["agent"] == "trend_pipeline"
    assert kw["model"] == "model-x"                         # ctx.settings.models.default


# -- fact_gate -----------------------------------------------------------------

def test_fact_gate_none_when_no_dossier_attr():
    assert scoring.fact_gate(SimpleNamespace()) is None      # getattr -> None -> {}


def test_fact_gate_none_when_dossier_none_or_empty():
    assert scoring.fact_gate(SimpleNamespace(dossier=None)) is None
    assert scoring.fact_gate(SimpleNamespace(dossier={})) is None
    assert scoring.fact_gate(SimpleNamespace(dossier={"other": 1})) is None   # no key_facts
    assert scoring.fact_gate(SimpleNamespace(dossier={"key_facts": []})) is None


def test_fact_gate_counts_only_dicts_with_source_url():
    facts = [
        {"source_url": "https://a.example/x"},   # verified
        {"source_url": ""},                       # falsy -> not verified
        {"source_url": None},                     # falsy -> not verified
        {"claim": "no url"},                      # missing -> not verified
        "a bare string",                          # non-dict -> not verified
        {"source_url": "https://b.example/y"},   # verified
    ]
    out = scoring.fact_gate(SimpleNamespace(dossier={"key_facts": facts}))
    assert out == {"verified": 2, "total": 6, "label": "2/6 verified"}


def test_fact_gate_all_verified():
    facts = [{"source_url": "u1"}, {"source_url": "u2"}]
    assert scoring.fact_gate(SimpleNamespace(dossier={"key_facts": facts})) == {
        "verified": 2, "total": 2, "label": "2/2 verified"}


def test_fact_gate_none_verified():
    facts = [{"claim": "x"}, {"claim": "y"}, {"claim": "z"}]
    assert scoring.fact_gate(SimpleNamespace(dossier={"key_facts": facts})) == {
        "verified": 0, "total": 3, "label": "0/3 verified"}
