"""Per-topic reader-session momentum (§13.19 F2) — pure."""
from switchboard.trends.sessions import compute_session_momentum, momentum_for_oems


def _rows(oem, weekly):
    """weekly: {iso_week: [sessions, ...]} → flat article rows for that OEM."""
    return [{"title": f"{oem} Model news update", "week": wk, "sessions": s}
            for wk, sess in weekly.items() for s in sess]


def test_rising_and_falling_momentum():
    rising = _rows("tesla", {"202601": [100, 100], "202602": [100, 100],
                             "202603": [200, 200], "202604": [200, 200]})
    assert compute_session_momentum(rising)["tesla"] > 0      # older 100 → recent 200

    falling = _rows("ford", {"202601": [200, 200], "202602": [200, 200],
                             "202603": [100, 100], "202604": [100, 100]})
    assert compute_session_momentum(falling)["ford"] < 0      # older 200 → recent 100


def test_min_articles_gate():
    thin = _rows("kia", {"202601": [100], "202602": [50]})    # only 2 articles
    assert "kia" not in compute_session_momentum(thin, min_articles=4)


def test_momentum_for_oems_averages_and_misses():
    mmap = {"tesla": 0.5, "ford": -0.4}
    assert momentum_for_oems(("tesla", "ford"), mmap) == 0.05   # (0.5 − 0.4)/2
    assert momentum_for_oems(("honda",), mmap) is None
