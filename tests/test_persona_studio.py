"""Persona Studio (§16.3) — recipe synthesis + anti-slop layer."""

import json
from types import SimpleNamespace

from switchboard import persona_studio
from switchboard.trends import generators, personas


def test_clean_dials_keeps_only_known_and_valid():
    d = persona_studio.clean_dials(
        {"wit": "Dry", "formality": "bogus", "nope": "x", "humor": "subtle"}
    )
    assert d == {"wit": "dry", "humor": "subtle"}


def test_build_synth_prompt_includes_ingredients():
    p = persona_studio.build_synth_prompt(
        name="Punchy",
        blend=[{"name": "Jane Doe", "features": {"voice": "wry", "tone": "bold"}}],
        dials={"wit": "biting"},
        notes="short sentences; never say 'boasts'",
    )
    assert "Punchy" in p
    assert "Jane Doe" in p and "voice=wry" in p
    assert "biting" in p
    assert "boasts" in p


def test_build_synth_prompt_empty_recipe_has_fallback():
    p = persona_studio.build_synth_prompt(name="", blend=[], dials={}, notes="")
    assert "invent a distinctive" in p.lower()


def test_coerce_features_normalizes_shape():
    f = persona_studio.coerce_features(
        {"voice": "  wry  ", "dos": "lead with the news", "bogus": 1}
    )
    assert f["voice"] == "wry"
    assert f["dos"] == ["lead with the news"]  # string coerced to 1-item list
    assert "bogus" not in f
    for k in ("voice", "tone", "sentence_rhythm", "structure", "formatting",
              "headline_style", "vocabulary", "dos", "donts"):
        assert k in f


def test_parse_synth_splits_features_and_sample():
    raw = json.dumps({
        "voice": "wry insider", "tone": "bold", "sentence_rhythm": "punchy",
        "structure": "lede then payoff", "formatting": "subheads",
        "headline_style": "curiosity gap", "vocabulary": "gearhead",
        "dos": ["open concrete"], "donts": ["no hedging"],
        "sample": "The V8 barks to life. No preamble.",
    })
    feats, sample = persona_studio.parse_synth(raw)
    assert feats["voice"] == "wry insider" and feats["dos"] == ["open concrete"]
    assert "barks to life" in sample


def test_has_features():
    assert not persona_studio.has_features(None)
    assert not persona_studio.has_features({"voice": "", "dos": []})
    assert persona_studio.has_features({"voice": "wry"})


def test_anti_slop_baked_into_generator_system():
    assert "it's worth noting" in generators._LLM_SYSTEM
    assert "game-changer" in generators._LLM_SYSTEM
    # base editorial instruction preserved
    assert "senior editor" in generators._LLM_SYSTEM


async def test_synthesize_persona_calls_llm_and_parses(monkeypatch):
    raw = json.dumps({
        "voice": "wry insider", "tone": "bold", "sentence_rhythm": "punchy",
        "structure": "lede then payoff", "formatting": "subheads",
        "headline_style": "curiosity gap", "vocabulary": "gearhead slang",
        "dos": ["open concrete"], "donts": ["no hedging"],
        "sample": "It idles like a threat. Then it isn't idling.",
    })

    seen = {}

    class FakeLLM:
        def __init__(self, ctx):
            pass

        async def complete(self, *, system, prompt, model=None, max_tokens=1024, agent="x"):
            seen["prompt"] = prompt
            seen["system"] = system
            return SimpleNamespace(text=raw, micros=1234)

    monkeypatch.setattr("switchboard.adapters.clients.llm.LLMClient", FakeLLM)
    ctx = SimpleNamespace(settings=SimpleNamespace(models=SimpleNamespace(default="m")))

    feats, sample, micros = await personas.synthesize_persona(
        ctx, brand="hotcars", name="Punchy",
        blend=[{"name": "Jane", "features": {"voice": "wry"}}],
        dials={"wit": "biting"}, notes="never hedge",
    )
    assert feats["voice"] == "wry insider" and micros == 1234
    assert "threat" in sample
    assert "BLEND THESE REAL WRITER VOICES" in seen["prompt"]  # blend fed in
    assert "biting" in seen["prompt"]  # dial fed in
