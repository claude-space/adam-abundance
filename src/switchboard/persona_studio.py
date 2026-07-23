"""Persona Studio (§16.3): synthesize a characterful, human-sounding house voice
from a *recipe* — a blend of the brand's real writer voices, a set of style dials,
and freeform character notes — into the same 9-key style-feature shape the writer
distiller emits (so the generator injects it identically).

Pure prompt-building + parsing here (unit-testable); the LLM call + persistence
live in ``trends.personas`` / the API layer.

The synthesis is explicitly tuned AGAINST generic "AI slop": it demands concrete,
opinionated, specific style — signature moves and real quirks — and bans hollow
descriptors in both the feature block and the demonstrated sample.
"""

from __future__ import annotations

from typing import Any

from . import style as style_mod

# The tunable style dials the Studio exposes. key -> (label, allowed values).
# The frontend renders these; the backend accepts any subset (unknown keys /
# values are dropped by ``clean_dials``).
DIALS: dict[str, tuple[str, tuple[str, ...]]] = {
    "wit": ("Wit", ("none", "dry", "playful", "biting")),
    "formality": ("Formality", ("street", "conversational", "polished", "buttoned-up")),
    "rhythm": ("Sentence rhythm", ("punchy", "varied", "flowing")),
    "opinion": ("Point of view", ("reported", "measured", "opinionated", "provocative")),
    "depth": ("Technical depth", ("accessible", "enthusiast", "expert")),
    "humor": ("Humor", ("none", "subtle", "frequent")),
    "sensory": ("Sensory detail", ("sparse", "moderate", "rich")),
    "warmth": ("Warmth", ("cool", "neutral", "warm")),
}


def clean_dials(raw: dict | None) -> dict[str, str]:
    """Keep only known dials set to an allowed value (lower-cased)."""
    out: dict[str, str] = {}
    for key, (_label, allowed) in DIALS.items():
        v = (raw or {}).get(key)
        if isinstance(v, str) and v.strip().lower() in allowed:
            out[key] = v.strip().lower()
    return out


# The 3 short blend descriptors we feed per writer — enough to fuse a sensibility
# without pasting whole guides.
def _blend_line(b: dict[str, Any]) -> str:
    f = b.get("features") or {}
    bits = [f"voice={f.get('voice', '')}", f"tone={f.get('tone', '')}",
            f"rhythm={f.get('sentence_rhythm', '')}", f"headlines={f.get('headline_style', '')}"]
    return f"- {b.get('name', '?')}: " + "; ".join(x for x in bits if not x.endswith('='))


SYNTH_SYSTEM = (
    "You are a masthead editor defining a NAMED house writing voice for a major "
    "automotive publication. You are given ingredients — excerpts of real staff "
    "writers' distilled styles to BLEND, a set of style dials, and freeform character "
    "notes. Fuse them into ONE coherent, distinctive HUMAN voice with taste and a point "
    "of view.\n\n"
    "Output STRICT JSON only (no prose, no code fence) with exactly these keys:\n"
    "  voice, tone, sentence_rhythm, structure, formatting, headline_style, vocabulary "
    "(each a short, specific string), dos and donts (arrays of <=5 short imperative "
    "strings), and sample (a 90-140 word paragraph WRITTEN IN THIS VOICE about a generic "
    "automotive topic, demonstrating the character — not describing it).\n\n"
    "Make it read like a specific person, never generic AI. Name concrete signature "
    "moves, cadences, and quirks. In BOTH the descriptors and the sample, BAN AI/corporate "
    "tells: 'in today's fast-paced world', 'it's worth noting', 'when it comes to', "
    "'nestled', 'boasts', 'a testament to', 'game-changer', 'elevate', 'delve', 'realm', "
    "'unleash', forced rule-of-three lists, 'not just X, but Y' constructions, hedging, and "
    "throat-clearing intros. Favor a strong POV, varied sentence length, concrete detail, "
    "and the occasional short fragment for punch."
)


def build_synth_prompt(*, name: str, blend: list[dict[str, Any]] | None,
                       dials: dict[str, str] | None, notes: str) -> str:
    """Compose the user prompt for one persona-synthesis call from the recipe."""
    parts: list[str] = [f"HOUSE VOICE NAME: {name.strip() or '(unnamed)'}", ""]
    if blend:
        parts.append("BLEND THESE REAL WRITER VOICES (fuse their sensibilities, do not copy):")
        parts += [_blend_line(b) for b in blend]
        parts.append("")
    if dials:
        parts.append("STYLE DIALS:")
        parts += [f"- {DIALS.get(k, (k,))[0]}: {v}" for k, v in dials.items()]
        parts.append("")
    if notes.strip():
        parts.append("CHARACTER NOTES (honor these — signature moves, pet phrases, hard nos):")
        parts.append(notes.strip())
        parts.append("")
    if not blend and not dials and not notes.strip():
        parts.append("(No extra ingredients — invent a distinctive, opinionated enthusiast voice.)")
        parts.append("")
    parts.append("Return the JSON now.")
    return "\n".join(parts)


def coerce_features(obj: Any) -> dict[str, Any]:
    """Normalise a features dict to the canonical 9-key shape (mirrors
    style.parse_style_features but for an already-parsed object) — used to sanitise
    features a client echoes back from a preview."""
    src = obj if isinstance(obj, dict) else {}
    out: dict[str, Any] = {}
    for k in style_mod.FEATURE_KEYS:
        v = src.get(k)
        if k in style_mod._LIST_KEYS:
            if isinstance(v, str):
                v = [v]
            out[k] = [str(x).strip() for x in (v or []) if str(x).strip()][:5]
        else:
            out[k] = str(v).strip() if isinstance(v, (str, int, float)) else ""
    return out


def parse_synth(raw: str) -> tuple[dict[str, Any], str]:
    """Split a synthesis response into (features, sample)."""
    features = style_mod.parse_style_features(raw)
    obj = style_mod._extract_json_object(raw)
    sample = ""
    if isinstance(obj, dict):
        sample = str(obj.get("sample") or "").strip()
    return features, sample


def has_features(features: dict[str, Any] | None) -> bool:
    return bool(features) and any((features or {}).get(k) for k in style_mod.FEATURE_KEYS)
