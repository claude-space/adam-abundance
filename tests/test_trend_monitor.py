"""Phase 8 (§16.2) trend activity lifecycle state machine."""
from switchboard.trends.monitor import compute_trend_state


def _series(vals):
    return [{"external_score": v} for v in vals]


def test_rising():
    r = compute_trend_state(_series([10, 12, 15, 20, 30, 45, 60]))
    assert r["state"] == "rising" and r["suppressed"] is False


def test_declining_auto_suppresses():
    r = compute_trend_state(_series([60, 55, 50, 40, 25, 15, 8]))
    assert r["state"] == "declining" and r["suppressed"] is True


def test_dormant_suppresses():
    r = compute_trend_state(_series([40, 30, 10, 4, 2, 1, 0]))
    assert r["state"] == "dormant" and r["suppressed"] is True


def test_peak_not_suppressed():
    r = compute_trend_state(_series([50, 55, 60, 58, 62, 59, 61]))
    assert r["state"] == "peak" and r["suppressed"] is False


def test_evergreen_exempt_from_suppression():
    r = compute_trend_state(_series([60, 55, 50, 40, 25, 15, 8]), evergreen=True)
    assert r["state"] == "declining" and r["suppressed"] is False


def test_re_acceleration_lifts_suppression():
    # was fading, now climbing again → rising, not suppressed
    r = compute_trend_state(_series([50, 40, 20, 10, 25, 45, 70]))
    assert r["state"] == "rising" and r["suppressed"] is False


def test_emerging_on_thin_history():
    r = compute_trend_state(_series([12]))
    assert r["state"] == "emerging" and r["suppressed"] is False


def test_onsite_sessions_fallback_when_no_external():
    # external absent → uses on-site sessions; a decline still suppresses
    series = [{"onsite_sessions": s} for s in [900, 800, 700, 400, 250, 120, 60]]
    r = compute_trend_state(series)
    assert r["state"] == "declining" and r["suppressed"] is True
