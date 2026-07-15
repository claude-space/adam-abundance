"""Phase 7 (§16.1) session-trends computation — acceptance math."""
from datetime import date, timedelta

from switchboard.session_trends import compute_session_trends, iso_week_start


def _rows(week_start, visits, depth=50, dur=30):
    return [{"date": (week_start + timedelta(days=i)).isoformat(),
             "visits": visits[i], "averageEngagedDepth": depth, "averageEngagedDuration": dur}
            for i in range(len(visits))]


def test_iso_week_start_is_monday():
    assert iso_week_start(date(2026, 7, 9)).weekday() == 0


def test_weekly_daily_deltas_and_flags():
    ws = date(2026, 7, 6)
    this_rows = _rows(ws, [100, 110, 120, 60, 130, 140, 150])       # sum 810, a -50% dip on day 4
    prev_rows = _rows(ws - timedelta(days=7), [90, 90, 90, 90, 90, 90, 100])  # sum 640

    r = compute_session_trends(brand="hotcars", week_start=ws,
                               this_week_rows=this_rows, prev_week_rows=prev_rows,
                               threshold_pct=25.0)
    v = r["metrics"]["visits"]
    assert len(v["series"]) == 7
    assert v["weekly"] == 810
    assert v["prev_weekly"] == 640
    assert v["wow_pct"] == 26.6                       # (810-640)/640
    assert v["peak"]["value"] == 150 and v["trough"]["value"] == 60

    # engaged depth is an average metric → mean of active days, not summed
    d = r["metrics"]["averageEngagedDepth"]
    assert d["weekly"] == 50.0 and d["wow_pct"] == 0.0

    kinds = {(f["metric"], f["kind"]) for f in r["flags"]}
    assert ("visits", "wow") in kinds                 # +25% week-over-week
    assert ("visits", "dod") in kinds                 # a >25% day-over-day swing
    assert not any(f["metric"] == "averageEngagedDepth" for f in r["flags"])
    assert r["iso_week"].startswith("2026-W")


def test_missing_days_zero_filled_and_no_baseline():
    ws = date(2026, 7, 6)
    rows = [{"date": (ws + timedelta(days=i)).isoformat(), "visits": 10} for i in range(3)]
    r = compute_session_trends(brand="hotcars", week_start=ws,
                               this_week_rows=rows, prev_week_rows=[])
    v = r["metrics"]["visits"]
    assert len(v["series"]) == 7                       # 4 missing days zero-filled
    assert v["weekly"] == 30
    assert v["wow_pct"] is None                        # no prior-week baseline
    assert r["flags"] == []                            # no baseline, flat → no flags
