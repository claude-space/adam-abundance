"""Writer-emulation style layer (PRD §16.3).

Distil an aggregate *style* profile from a brand's top writers' published work so
the AI drafter can match house voice. This is a STYLE layer only — never a
byline, never a licence to invent facts — and the human fact-check / outline / QA
gates stay mandatory (§13.16).

The helpers here are pure (no network, no DB, no LLM) so exemplar selection,
feature parsing, and prompt rendering are unit-testable in isolation. The
scraping + LLM distillation + persistence live in the Analytics agent
(`_style_profile`); the generator reads the persisted profile at draft time.
"""

from __future__ import annotations

import json
from typing import Any

# The style features we ask the model to extract and then persist. Deliberately
# small and concrete so the generator can fold them straight into a prompt.
FEATURE_KEYS = (
    "voice", "tone", "sentence_rhythm", "structure",
    "formatting", "headline_style", "vocabulary", "dos", "donts",
)
_LIST_KEYS = ("dos", "donts")

STYLE_SYSTEM = (
    "You are a copy chief analysing a publication's house style. From the article "
    "excerpts you are given, extract the SHARED, reusable style conventions — how "
    "these writers write, NOT what they wrote about. Output STRICT JSON only (no "
    "prose, no code fence) with exactly these keys: " + ", ".join(FEATURE_KEYS) + ". "
    "Each value is a short string, except 'dos' and 'donts' which are arrays of at "
    "most 5 short imperative strings. Characterise voice, tone, typical sentence "
    "rhythm, article structure, formatting habits (subheads, lists, pull-quotes), "
    "headline conventions, and characteristic vocabulary. Never reference a specific "
    "car, person, brand, or event — style only."
)


def select_exemplars(top_authors: list[str], rows: list[dict[str, Any]], *,
                     per_author: int = 2, cap: int = 8) -> list[dict[str, Any]]:
    """Choose a diverse exemplar set: each top author's best-performing articles
    (by sessions), interleaved by rank so the corpus stays spread across authors
    under ``cap``. De-dupes by URL; ignores rows without an author or URL."""
    by_author: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        author = (r.get("author") or "").strip()
        url = (r.get("url") or "").strip()
        if not author or not url:
            continue
        by_author.setdefault(author, []).append({
            "author": author, "title": (r.get("title") or "").strip(),
            "url": url, "sessions": int(r.get("sessions") or 0),
        })
    ordered = [a for a in top_authors if a in by_author]
    pools = {a: sorted(by_author[a], key=lambda x: x["sessions"], reverse=True)[:max(per_author, 0)]
             for a in ordered}
    picks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rank in range(per_author):
        for a in ordered:
            pool = pools[a]
            if rank < len(pool) and pool[rank]["url"] not in seen:
                picks.append(pool[rank])
                seen.add(pool[rank]["url"])
                if len(picks) >= cap:
                    return picks
    return picks


def build_distill_prompt(brand: str, exemplars: list[dict[str, Any]], *,
                         max_chars: int = 2500) -> str:
    """The user prompt for distillation: the brand plus each exemplar's byline,
    title, and body text (truncated)."""
    parts = [
        f"BRAND: {brand}",
        f"EXEMPLARS: {len(exemplars)} top-performing articles by this brand's "
        "highest-indexing writers.",
        "",
    ]
    for i, ex in enumerate(exemplars, 1):
        parts += [
            f"--- EXEMPLAR {i} — {ex.get('author', '?')} — {ex.get('title', '')} ---",
            (ex.get("text") or "")[:max_chars],
            "",
        ]
    parts.append("Return the JSON style profile now.")
    return "\n".join(parts)


def parse_style_features(raw: str) -> dict[str, Any]:
    """Extract and normalise the JSON style features from the model response.
    Tolerant of ```json fences and stray prose around the object. Always returns
    every FEATURE_KEY (strings default '', list keys default [])."""
    obj = _extract_json_object(raw)
    out: dict[str, Any] = {}
    for k in FEATURE_KEYS:
        v = obj.get(k) if isinstance(obj, dict) else None
        if k in _LIST_KEYS:
            if isinstance(v, str):
                v = [v]
            out[k] = [str(x).strip() for x in (v or []) if str(x).strip()][:5]
        else:
            out[k] = str(v).strip() if isinstance(v, (str, int, float)) else ""
    return out


def _extract_json_object(raw: str) -> Any:
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        # ```json\n{...}\n``` → keep the fenced body
        inner = s.split("```")
        s = inner[1] if len(inner) >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except (ValueError, TypeError):
        return None


_GUIDE_LABELS = (
    ("voice", "Voice"), ("tone", "Tone"), ("sentence_rhythm", "Sentence rhythm"),
    ("structure", "Structure"), ("formatting", "Formatting"),
    ("headline_style", "Headlines"), ("vocabulary", "Vocabulary"),
)


def style_guide_text(features: dict[str, Any] | None) -> str:
    """Render persisted features into a compact STYLE GUIDE block for the
    generator's system prompt. Returns '' when there's nothing usable, so the
    caller can treat 'no profile' and 'empty profile' identically."""
    if not features:
        return ""
    lines: list[str] = []
    for key, label in _GUIDE_LABELS:
        v = features.get(key)
        if v:
            lines.append(f"- {label}: {v}")
    dos = features.get("dos") or []
    donts = features.get("donts") or []
    if dos:
        lines.append("- Do: " + "; ".join(dos))
    if donts:
        lines.append("- Don't: " + "; ".join(donts))
    if not lines:
        return ""
    return ("HOUSE STYLE GUIDE (match this publication's established voice — style "
            "only; never bend or invent a fact to fit it):\n" + "\n".join(lines))
