"""Article session trends (PRD §16.1) — weekly rollup + daily series with
day-over-day and week-over-week deltas plus notable-movement flags.

Pure + deterministic (no I/O) so the acceptance math is unit-testable; the
Analytics agent (§6.5) supplies the daily rows (from Sentinel) and persists the
result as a ``session_trends`` metric entry + ``flag`` entries.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

# Metrics we trend. Sentinel `traffic` daily rows expose these as distinct keys;
# the ``average*`` metrics are per-day averages (rolled up as a mean of active
# days), the rest (``sessions``/``views``/``visits``) are counts (summed for the
# weekly total).
DEFAULT_METRICS: tuple[str, ...] = (
    "sessions", "views", "visits", "averageEngagedDepth", "averageEngagedDuration")
_AVG_PREFIX = "average"


def iso_week_start(d: date) -> date:
    """Monday of ``d``'s ISO week."""
    return d - timedelta(days=d.weekday())


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _by_date(rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows or []:
        key = str(r.get("date") or "")[:10]
        if key:
            out[key] = r
    return out


def _weekly(series_vals: list[float], metric: str) -> float:
    """Sum for count metrics; mean of active (non-zero) days for averages."""
    if metric.startswith(_AVG_PREFIX):
        active = [v for v in series_vals if v]
        return round(sum(active) / len(active), 2) if active else 0.0
    return round(sum(series_vals), 2)


def _pct(cur: float, prev: float) -> float | None:
    """Percent change cur vs prev; None when there's no baseline."""
    if not prev:
        return None
    return round(100.0 * (cur - prev) / prev, 1)


def compute_session_trends(
    *,
    brand: str,
    week_start: date,
    this_week_rows: list[dict[str, Any]],
    prev_week_rows: list[dict[str, Any]],
    metrics: tuple[str, ...] = DEFAULT_METRICS,
    threshold_pct: float = 25.0,
) -> dict[str, Any]:
    """Session-trends payload for one brand + ISO week (Monday = ``week_start``).

    Returns::

        {kind, brand, week_start, iso_week, threshold_pct,
         metrics: { <metric>: {series:[{date,value}]*7, weekly, prev_weekly,
                                wow_pct, dod:[{date,pct}], peak, trough} },
         flags: [{metric, kind:'wow'|'dod', pct, direction, date?}]}
    """
    days = [week_start + timedelta(days=i) for i in range(7)]
    prev_days = [d - timedelta(days=7) for d in days]  # aligned prior week
    tw, pw = _by_date(this_week_rows), _by_date(prev_week_rows)

    out_metrics: dict[str, Any] = {}
    flags: list[dict[str, Any]] = []
    for m in metrics:
        # value is None for days with NO Sentinel row (unknown) — kept distinct
        # from a real 0 so gaps don't masquerade as a -100% crash in the deltas.
        series: list[dict[str, Any]] = []
        for d in days:
            row = tw.get(d.isoformat())
            series.append({"date": d.isoformat(),
                           "value": round(_num(row.get(m)), 2) if row is not None else None})
        present = [pt["value"] for pt in series if pt["value"] is not None]
        prev_present = [round(_num(pw[d.isoformat()].get(m)), 2) for d in prev_days if d.isoformat() in pw]

        weekly = _weekly(present, m)
        prev_weekly = _weekly(prev_present, m)
        wow = _pct(weekly, prev_weekly)
        dod: list[dict[str, Any]] = []
        for i in range(1, 7):
            cur, prv = series[i]["value"], series[i - 1]["value"]
            dod.append({"date": days[i].isoformat(),
                        "pct": _pct(cur, prv) if (cur is not None and prv is not None) else None})
        withval = [pt for pt in series if pt["value"] is not None]
        peak = max(withval, key=lambda p: p["value"]) if withval else None
        trough = min(withval, key=lambda p: p["value"]) if withval else None

        out_metrics[m] = {"series": series, "weekly": weekly, "prev_weekly": prev_weekly,
                          "wow_pct": wow, "dod": dod, "peak": peak, "trough": trough}

        if wow is not None and abs(wow) >= threshold_pct:
            flags.append({"metric": m, "kind": "wow", "pct": wow,
                          "direction": "up" if wow > 0 else "down"})
        swings = [x for x in dod if x["pct"] is not None]
        if swings:
            biggest = max(swings, key=lambda x: abs(x["pct"]))
            if abs(biggest["pct"]) >= threshold_pct:
                flags.append({"metric": m, "kind": "dod", "pct": biggest["pct"],
                              "date": biggest["date"], "direction": "up" if biggest["pct"] > 0 else "down"})

    yr, wk, _ = week_start.isocalendar()
    return {
        "kind": "session_trends", "brand": brand,
        "week_start": week_start.isoformat(), "iso_week": f"{yr}-W{wk:02d}",
        "threshold_pct": threshold_pct, "metrics": out_metrics, "flags": flags,
    }
