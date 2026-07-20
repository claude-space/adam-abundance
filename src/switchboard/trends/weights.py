"""Persistence for the operator-tunable Trend Score weights (§13.19).

The scoring math and its shipped defaults live in ``detector`` (pure, no DB);
this module reads/writes the ``trend_score_weight`` overrides. Latest row per
key wins, so edits are an append-only audit trail.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..db.models import TrendScoreWeight
from .detector import DEFAULT_SCORE_WEIGHTS, SCORE_WEIGHT_META, resolve_weights


async def load_overrides(session: Any) -> dict[str, float]:
    """Persisted overrides that genuinely differ from the shipped default (latest
    row per key). A value pinned back to its default — e.g. after a reset — is
    dropped, so callers can treat "has overrides" as "is customized" truthfully.
    Empty ⇒ all defaults."""
    rows = (await session.execute(
        select(TrendScoreWeight.key, TrendScoreWeight.value, TrendScoreWeight.effective_at)
        .order_by(TrendScoreWeight.effective_at.asc())
    )).all()
    latest: dict[str, float] = {}
    for key, value, _ in rows:               # asc order ⇒ last write wins
        if key in DEFAULT_SCORE_WEIGHTS:
            latest[key] = float(value)
    return {k: v for k, v in latest.items()
            if abs(v - DEFAULT_SCORE_WEIGHTS[k]) > 1e-9}


async def load_effective(session: Any) -> dict[str, float]:
    """Defaults with any persisted overrides applied — what the scorer uses."""
    return resolve_weights(await load_overrides(session))


async def save_weights(session: Any, values: dict[str, float], *, updated_by: str | None = None) -> list[str]:
    """Persist a new row for each known, changed weight (clamped to its meta
    range). Returns the list of keys actually written."""
    ranges = {m["key"]: (float(m["min"]), float(m["max"])) for m in SCORE_WEIGHT_META}
    current = resolve_weights(await load_overrides(session))
    written: list[str] = []
    for key, raw in (values or {}).items():
        if key not in DEFAULT_SCORE_WEIGHTS or raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        lo, hi = ranges.get(key, (0.0, 100.0))
        val = max(lo, min(hi, round(val, 4)))
        if abs(val - current.get(key, DEFAULT_SCORE_WEIGHTS[key])) < 1e-9:
            continue                          # unchanged → no new row
        session.add(TrendScoreWeight(key=key, value=val, updated_by=updated_by))
        written.append(key)
    if written:
        await session.flush()
    return written


async def reset_weights(session: Any, *, updated_by: str | None = None) -> int:
    """Pin every weight back to its shipped default (writes a row per key that
    currently differs). Returns how many were reset."""
    written = await save_weights(session, dict(DEFAULT_SCORE_WEIGHTS), updated_by=updated_by)
    return len(written)
