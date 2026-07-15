"""Trend activity lifecycle (PRD §16.2) — classify a trend's state from its
activity series and decide soft auto-suppression. Pure + deterministic (no I/O),
so the state machine is unit-testable; the scout/Research caller supplies the
`trend_activity` series and persists the result onto the `trend` row.

State machine: emerging → rising → peak → declining → dormant. A declining/dormant
trend that re-accelerates flips back to rising (suppression lifts automatically).
Evergreen trends are exempt. Suppression is SOFT + forward-only: it stops *new*
sourcing/posting for a fading trend — it never unpublishes anything.
"""

from __future__ import annotations

from typing import Any

# Detection thresholds (config, not code — the caller may override from TrendConfig/env).
RISE_PCT = 0.25          # recent window vs earlier window up   >= 25% → rising
DECLINE_PCT = 0.30       # ...                              down >= 30% → declining (suppress trigger)
DORMANT_FLOOR = 5.0      # recent-window average activity <= this      → dormant
PEAK_FRAC = 0.80         # recent average >= this fraction of the series max (and flat) → peak
WINDOW = 3               # days per comparison window

_DECLINE_STATES = ("declining", "dormant")


def _avg(xs: list[float]) -> float:
    vals = [float(x) for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _activity(point: dict[str, Any]) -> float:
    """One scalar per day: external interest when present, else on-site sessions."""
    ext = point.get("external_score")
    if ext is not None:
        return float(ext)
    return float(point.get("onsite_sessions") or 0)


def compute_trend_state(
    series: list[dict[str, Any]],
    *,
    evergreen: bool = False,
    rise_pct: float = RISE_PCT,
    decline_pct: float = DECLINE_PCT,
    dormant_floor: float = DORMANT_FLOOR,
    peak_frac: float = PEAK_FRAC,
    window: int = WINDOW,
) -> dict[str, Any]:
    """Classify ``series`` (oldest→newest activity samples, each carrying
    ``external_score`` and/or ``onsite_sessions``).

    Returns ``{state, suppressed, reason, recent_avg, earlier_avg, delta_pct}``.
    """
    pts = [_activity(p) for p in series]
    if len(pts) < 2:
        return {"state": "emerging", "suppressed": False, "reason": "insufficient history",
                "recent_avg": round(pts[-1], 2) if pts else 0.0, "earlier_avg": None, "delta_pct": None}

    recent = pts[-window:]
    earlier = pts[-2 * window:-window] or pts[:-window] or pts[:1]
    recent_avg, earlier_avg = _avg(recent), _avg(earlier)
    peak_val = max(pts)
    delta = ((recent_avg - earlier_avg) / earlier_avg) if earlier_avg else None

    if recent_avg <= dormant_floor:
        state = "dormant"
    elif delta is not None and delta <= -decline_pct:
        state = "declining"
    elif delta is not None and delta >= rise_pct:
        state = "rising"
    elif recent_avg >= peak_frac * peak_val:
        state = "peak"
    else:
        state = "emerging"

    # Suppression is fully determined by state (re-acceleration → rising → not
    # in the decline set → un-suppressed). Evergreen is always exempt.
    suppressed = (not evergreen) and state in _DECLINE_STATES

    reason = f"{state}: recent {recent_avg:.1f} vs earlier {earlier_avg:.1f}"
    if delta is not None:
        reason += f" ({delta * 100:+.0f}%)"
    return {"state": state, "suppressed": suppressed, "reason": reason,
            "recent_avg": round(recent_avg, 2), "earlier_avg": round(earlier_avg, 2),
            "delta_pct": round(delta * 100, 1) if delta is not None else None}
