"""Per-topic reader-session momentum (§13.19, the F2 "performed well/poorly"
signal). Are OUR sessions on a given topic (keyed by OEM/make) trending up or
down over recent weeks? This feeds the trend scorer's same-topic momentum ``q``
with real reader data instead of the lifecycle proxy: "competitors covered it AND
our readers are tiring of the topic" then lowers the score, measurably.

Pure + deterministic — the scout supplies the consum rows; nothing here does I/O.
"""

from __future__ import annotations

from typing import Any, Iterable

from .detector import oems


def compute_session_momentum(rows: Iterable[dict[str, Any]], *,
                             min_articles: int = 4) -> dict[str, float]:
    """``rows``: ``[{title, week, sessions}]`` where ``week`` is a sortable ISO
    week key (e.g. "202629"). Returns ``{oem: momentum∈[-1,1]}`` — the recent-half
    vs older-half change in weekly average sessions-per-article for each OEM.
    Rising→+, falling→−. OEMs seen in fewer than ``min_articles`` are omitted
    (too little data to trust)."""
    by: dict[str, dict[str, list[float]]] = {}
    counts: dict[str, int] = {}
    for r in rows:
        wk = str(r.get("week") or "")
        if not wk:
            continue
        s = float(r.get("sessions") or 0)
        for o in set(oems(r.get("title", ""))):
            by.setdefault(o, {}).setdefault(wk, []).append(s)
            counts[o] = counts.get(o, 0) + 1

    out: dict[str, float] = {}
    for oem, weeks in by.items():
        if counts.get(oem, 0) < min_articles or len(weeks) < 2:
            continue
        wavg = {w: sum(v) / len(v) for w, v in weeks.items()}   # avg sessions/article per week
        ordered = sorted(wavg)
        mid = max(1, len(ordered) // 2)
        older, recent = ordered[:mid], (ordered[mid:] or ordered[-1:])
        o_avg = sum(wavg[w] for w in older) / len(older)
        r_avg = sum(wavg[w] for w in recent) / len(recent)
        if o_avg <= 0:
            continue
        out[oem] = round(max(-1.0, min(1.0, (r_avg - o_avg) / o_avg)), 3)
    return out


def momentum_for_oems(oem_anchor: Iterable[str], mmap: dict[str, float]) -> float | None:
    """Average session momentum across a cluster's OEM anchor, or None when we
    have no session signal for any of its OEMs (caller falls back to the proxy)."""
    vals = [mmap[o] for o in oem_anchor if o in mmap]
    return round(sum(vals) / len(vals), 3) if vals else None
