"""Trend-pipeline lifecycle safety tests (docs/trend-pipeline.md).
Dependency-free — the rules module is pure stdlib. These encode the PRD §8
posture: no self-approval, no skipping the human gates."""

from switchboard.trends import lifecycle


def _raises(fn, *args):
    try:
        fn(*args)
    except lifecycle.LifecycleError:
        return True
    return False


def test_scout_and_system_cannot_approve():
    for actor in ("", "  ", "trend_scout", "scout", "system", "orchestrator", "SCHEDULER"):
        assert _raises(lifecycle.validate_actor, actor), actor
    lifecycle.validate_actor("andrew.marks@valnetinc.com")  # a human is fine


def test_pipeline_happy_path():
    for cur, new in [("pending_approval", "approved"), ("approved", "generating"),
                     ("generating", "previews_ready"), ("previews_ready", "published"),
                     ("published", "closed")]:
        lifecycle.validate_pipeline_transition(cur, new)


def test_pipeline_cannot_skip_the_approval_gate():
    assert _raises(lifecycle.validate_pipeline_transition, "pending_approval", "generating")
    assert _raises(lifecycle.validate_pipeline_transition, "pending_approval", "published")
    assert _raises(lifecycle.validate_pipeline_transition, "declined", "approved")
    assert _raises(lifecycle.validate_pipeline_transition, "closed", "generating")
    assert _raises(lifecycle.validate_pipeline_transition, "expired", "approved")


def test_job_happy_path_and_regenerate_loop():
    for cur, new in [("queued", "running"), ("running", "preview_ready"),
                     ("preview_ready", "approved"), ("approved", "published")]:
        lifecycle.validate_job_transition(cur, new)
    # regenerate: preview_ready/approved/rejected/failed -> queued
    for cur in ("preview_ready", "approved", "rejected", "failed"):
        lifecycle.validate_job_transition(cur, "queued")


def test_job_cannot_publish_without_editor_approval():
    assert _raises(lifecycle.validate_job_transition, "queued", "published")
    assert _raises(lifecycle.validate_job_transition, "running", "published")
    assert _raises(lifecycle.validate_job_transition, "preview_ready", "published")
    assert _raises(lifecycle.validate_job_transition, "rejected", "published")
    assert _raises(lifecycle.validate_job_transition, "published", "queued")  # terminal


def test_trend_transitions():
    for cur, new in [("detected", "proposed"), ("detected", "dossier_building"),
                     ("dossier_building", "proposed"), ("proposed", "approved"),
                     ("proposed", "declined"), ("approved", "completed"),
                     ("proposed", "expired"), ("detected", "dismissed")]:
        lifecycle.validate_trend_transition(cur, new)
    # terminal trends cannot be resurrected
    for cur in ("dismissed", "declined", "expired", "completed"):
        assert _raises(lifecycle.validate_trend_transition, cur, "approved"), cur
        assert _raises(lifecycle.validate_trend_transition, cur, "proposed"), cur
    # no-op transitions are fine
    lifecycle.validate_trend_transition("approved", "approved")


def test_closed_pipeline_stays_closed():
    for status in ("declined", "closed", "expired"):
        assert _raises(lifecycle.require_open_pipeline, status), status
    for status in lifecycle.PIPELINE_OPEN_STATUSES:
        lifecycle.require_open_pipeline(status)


def test_content_types_validated():
    assert lifecycle.validate_content_types(["Article", " social_post "]) == ["article", "social_post"]
    assert lifecycle.validate_content_types(["article", "article"]) == ["article"]
    assert _raises(lifecycle.validate_content_types, ["blogspam"])
    assert _raises(lifecycle.validate_content_types, [])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
