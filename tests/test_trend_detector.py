"""Trend detector safety tests (docs/trend-pipeline.md). Dependency-free —
clustering and scoring are pure stdlib."""

from datetime import datetime, timedelta, timezone

from switchboard.trends import detector


def _item(title, source="outlet", url=None, hours_ago=1.0, origin="rss"):
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {"origin": origin, "source": source, "title": title,
            "url": url or f"https://{source}.example.com/{abs(hash(title))}",
            "published_at": when.isoformat(), "snippet": ""}


def test_same_story_clusters_across_outlets():
    items = [
        _item("Tesla recalls 300,000 Model Y over steering fault", "caranddriver"),
        _item("Tesla Model Y recall: steering fault hits 300k vehicles", "motor1"),
        _item("Massive Tesla recall covers Model Y steering issue", "carscoops"),
        _item("Honda unveils new hybrid Civic for 2027", "thedrive"),
    ]
    clusters = detector.cluster_signals(items)
    sizes = sorted(len(c.items) for c in clusters)
    assert sizes == [1, 3]
    big = max(clusters, key=lambda c: len(c.items))
    assert len(big.sources) == 3
    assert "tesla" in big.oem_anchor


def test_duplicate_urls_are_deduped():
    a = _item("Tesla recalls Model Y", "caranddriver", url="https://x.com/a")
    clusters = detector.cluster_signals([a, dict(a)])
    assert sum(len(c.items) for c in clusters) == 1


def test_cluster_key_is_stable_across_orderings():
    items = [
        _item("Tesla recalls 300,000 Model Y over steering fault", "caranddriver"),
        _item("Tesla Model Y recall: steering fault hits 300k vehicles", "motor1"),
    ]
    k1 = detector.cluster_signals(items)[0].cluster_key()
    k2 = detector.cluster_signals(list(reversed(items)))[0].cluster_key()
    assert k1 == k2 != "untitled"


def test_score_rewards_novelty_gap_and_breaking():
    items = [
        _item("Tesla recalls 300,000 Model Y over steering fault", s, hours_ago=h)
        for s, h in [("caranddriver", 3), ("motor1", 2), ("carscoops", 1), ("insideevs", 0.5)]
    ]
    cluster = detector.cluster_signals(items)[0]
    score, breakdown = detector.score_cluster(cluster, covered=False)
    assert "outlet_breadth" not in breakdown           # replaced by novelty
    assert breakdown["novelty"] == 7.2                 # 4 outlets: 18·(1 − 3/5)
    assert breakdown["coverage_gap"] == 15.0
    assert breakdown["breaking"] == 12.0               # "recalls"
    assert breakdown["velocity"] > 0

    covered_score, covered_bd = detector.score_cluster(cluster, covered=True)
    assert covered_bd["coverage_gap"] == 0.0
    assert covered_score < score

    # Novelty: the SAME story on fewer outlets (less published) scores its
    # novelty higher — the "get ahead of it" signal.
    scarce = detector.cluster_signals(items[:2])[0]
    _s2, bd2 = detector.score_cluster(scarce, covered=False)
    assert bd2["novelty"] > breakdown["novelty"]       # 2 outlets: 18·(1 − 1/5) = 14.4


def test_coverage_penalty_only_when_covered_and_fading():
    # A well-covered story (many outlets → high saturation).
    items = [_item("Tesla Model Y refresh spied testing again", s, hours_ago=h)
             for s, h in [("a", 3), ("b", 2.5), ("c", 2), ("d", 1.5), ("e", 1), ("f", 0.5)]]
    cluster = detector.cluster_signals(items)[0]

    # Covered + doing WELL (positive momentum) → no saturation penalty (neutral).
    _s_well, well = detector.score_cluster(cluster, topic_momentum=0.6)
    assert "saturation_penalty" not in well

    # Covered + doing POORLY (negative momentum) → a penalty appears and lowers the score.
    s_poor, poor = detector.score_cluster(cluster, topic_momentum=-0.8)
    assert poor["saturation_penalty"] < 0
    assert s_poor < _s_well

    # Not covered (few outlets) → momentum irrelevant, novelty dominates, no penalty.
    scarce = detector.cluster_signals(items[:1] + [_item("unrelated honda thing", "z")])
    scarce = [c for c in scarce if len(c.items) == 1][0]
    _s, bd = detector.score_cluster(scarce, topic_momentum=-0.9)
    assert "saturation_penalty" not in bd


def test_theme_fatigue_penalty():
    items = [_item("Kia EV9 GT unveiled with 500 hp", s) for s in ("motor1", "thedrive")]
    cluster = detector.cluster_signals(items)[0]
    _base_s, base = detector.score_cluster(cluster)
    assert "theme_fatigue" not in base
    s_fatigued, bd = detector.score_cluster(cluster, theme_fatigue=0.5)
    assert bd["theme_fatigue"] == -7.5                 # −15 · 0.5
    assert s_fatigued < _base_s


def test_topic_similarity():
    items = [_item("Tesla recalls 300,000 Model Y over steering fault", "motor1"),
             _item("Tesla Model Y recall hits 300k vehicles", "carscoops")]
    cluster = detector.cluster_signals(items)[0]
    same = detector.topic_similarity(cluster, detector.tokens("Tesla Model Y recall grows"), ("tesla",))
    diff = detector.topic_similarity(cluster, detector.tokens("Honda Civic hybrid first drive review"))
    assert same > diff
    assert 0.0 <= diff <= same <= 1.0


def test_corroboration_scales_with_independent_monitors():
    items = [_item("Kia reveals EV9 GT with 500 hp", s) for s in ("motor1", "thedrive")]
    cluster = detector.cluster_signals(items)[0]

    _base_s, base = detector.score_cluster(cluster)
    assert "corroboration" not in base                 # our sourcing only — no bonus

    _one_s, one = detector.score_cluster(cluster, corroborating_monitors=["hc_viral_hits"])
    assert one["corroboration"] == 15.0                # one external monitor agrees

    _two_s, two = detector.score_cluster(
        cluster, corroborating_monitors=["hc_viral_hits", "daily_reporting"])
    assert two["corroboration"] == 30.0                # both agree → capped max

    # Back-compat: the old boolean still maps to a single HC-Viral corroboration.
    _c_s, compat = detector.score_cluster(cluster, corroborated=True)
    assert compat["corroboration"] == 15.0


def test_watchlist_boost_and_stale_penalty():
    items = [_item("Ford F-150 production halts at Michigan plant", s, hours_ago=80)
             for s in ("motor1", "thedrive")]
    cluster = detector.cluster_signals(items)[0]
    _score, bd = detector.score_cluster(cluster, watchlist=("f-150",))
    assert bd["watchlist"] == 15.0
    assert bd.get("stale_penalty") == -10.0


def test_evergreen_is_not_breaking():
    assert detector.is_breaking_text("Tesla recalls 300k cars")
    assert not detector.is_breaking_text("2026 Tesla Model Y review: first drive")
    assert not detector.is_breaking_text("The ten best trucks, ranked")


def test_covered_by_titles_matches_our_coverage():
    items = [_item("Tesla recalls 300,000 Model Y over steering fault", "motor1"),
             _item("Tesla Model Y recall grows to 300k vehicles", "carscoops")]
    cluster = detector.cluster_signals(items)[0]
    assert detector.covered_by_titles(cluster, ["Tesla Recalls 300,000 Model Y Over Steering Fault"])
    assert not detector.covered_by_titles(cluster, ["Honda Civic hybrid first look"])


def test_score_is_bounded_0_100():
    items = [_item(f"Tesla recall crash lawsuit reveal {i}", f"outlet{i}", hours_ago=0.2)
             for i in range(9)]
    cluster = detector.cluster_signals(items)[0]
    score, _ = detector.score_cluster(cluster, watchlist=("tesla",), covered=False)
    assert 0 <= score <= 100


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
