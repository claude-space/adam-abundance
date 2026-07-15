"""Phase 9 (§16.3) top-writer normalization."""
from switchboard.writers import normalize_writers


def _arts(author, category, intent, sessions, count):
    return [{"author": author, "category": category, "intent": intent, "sessions": sessions}
            for _ in range(count)]


def test_normalization_controls_for_beat_and_min_floor():
    arts = (
        _arts("A", "hot", "feed", 1000, 6)          # top beat, merely average within it
        + _arts("C", "hot", "feed", 1000, 5)
        + _arts("E", "hot", "feed", 1000, 3)        # below the min-article floor → excluded
        + _arts("B", "niche", "evergreen", 300, 6)  # low traffic, but 1.67x its cohort
        + _arts("D", "niche", "evergreen", 60, 6)   # underperforms its cohort
    )
    res = normalize_writers(arts, min_articles=5, top_n=2)
    by = {w["author"]: w for w in res}

    assert "E" not in by                                   # min-article floor
    # B beats its cohort → ranks above A despite far lower raw sessions
    assert by["B"]["norm_score"] > by["A"]["norm_score"]
    assert by["B"]["avg_sessions"] < by["A"]["avg_sessions"]
    assert res[0]["author"] == "B"
    assert by["B"]["is_top"] is True
    assert by["D"]["is_top"] is False
    assert round(by["B"]["norm_score"], 2) == 1.67        # 300 / 180 cohort mean


def test_top_n_flagging():
    arts = []
    for i, s in enumerate([500, 400, 300, 200, 100]):
        arts += _arts(f"w{i}", "cat", "feed", s, 5)
    res = normalize_writers(arts, min_articles=5, top_n=3)
    tops = [w["author"] for w in res if w["is_top"]]
    assert len(tops) == 3 and res[0]["author"] == "w0"
