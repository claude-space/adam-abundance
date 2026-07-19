"""Artifact quality scoring (§16 Distribution).

An LLM "critic" pass over a generated draft that produces the quality score +
the five-factor breakdown the console's artifact-review UI shows (Factuality /
Editorial fit / Freshness / SEO ceiling / Brand voice). The result is stored on
the job's ``preview_meta['quality']`` so the ``/api/artifacts`` surface reads it
back without re-scoring. Soft by design: any failure returns ``None`` and never
blocks generation.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..adapters.clients.llm import LLMClient
from ..context import RunContext

# Factor weights — MUST match the SPA's scoreBreakdown display (30/25/20/15/10).
WEIGHTS: dict[str, float] = {
    "factuality": 0.30,
    "editorial_fit": 0.25,
    "freshness": 0.20,
    "seo_ceiling": 0.15,
    "brand_voice": 0.10,
}

_SYSTEM = (
    "You are a demanding but fair editorial quality critic for {brand}, an "
    "automotive publication. Score the draft on five factors, 0-100 each:\n"
    "- factuality: are claims specific, sourced, and plausibly true? penalise vague or unverifiable statements.\n"
    "- editorial_fit: does it read like a professional automotive news/review outlet — structure, clarity, hook?\n"
    "- freshness: is the angle timely and non-generic?\n"
    "- seo_ceiling: headline and keyword strength / search potential.\n"
    "- brand_voice: consistency of voice and tone for the brand.\n"
    "Reply with ONLY compact JSON, no prose:\n"
    '{{"factuality":0,"editorial_fit":0,"freshness":0,"seo_ceiling":0,"brand_voice":0,"note":"one short sentence"}}'
)


def _clamp(v: Any) -> int | None:
    try:
        return max(0, min(100, int(round(float(v)))))
    except (TypeError, ValueError):
        return None


def _parse_json(text: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


async def score_draft(ctx: RunContext, brand: str, content_type: str, body: str) -> dict[str, Any] | None:
    """LLM-score a draft → ``{score, breakdown{5 factors}, note, micros}``; None on failure."""
    if not (body or "").strip():
        return None
    llm = LLMClient(ctx)
    try:
        result = await llm.complete(
            system=_SYSTEM.format(brand=brand),
            prompt=f"CONTENT TYPE: {content_type}\n\nDRAFT:\n{body[:6000]}",
            model=ctx.settings.models.default,
            max_tokens=300,
            agent="trend_pipeline",
        )
    except Exception:  # noqa: BLE001 — scoring is best-effort, never blocks the pipeline
        return None
    data = _parse_json(getattr(result, "text", "") or "")
    if not data:
        return None
    breakdown = {k: _clamp(data.get(k)) for k in WEIGHTS}
    if any(v is None for v in breakdown.values()):
        return None
    score = round(sum(breakdown[k] * w for k, w in WEIGHTS.items()))
    return {
        "score": score,
        "breakdown": breakdown,
        "note": str(data.get("note") or "")[:200],
        "micros": getattr(result, "micros", 0) or 0,
    }


def fact_gate(trend: Any) -> dict[str, Any] | None:
    """Fact-gate signal from the trend dossier's ``key_facts``: a fact counts as
    verified when it carries a ``source_url``. Returns ``{verified, total, label}``
    or None when the dossier has no facts."""
    dossier = getattr(trend, "dossier", None) or {}
    facts = dossier.get("key_facts") or []
    total = len(facts)
    if total == 0:
        return None
    verified = sum(1 for f in facts if isinstance(f, dict) and f.get("source_url"))
    return {"verified": verified, "total": total, "label": f"{verified}/{total} verified"}
