"""Pure lifecycle rules for the trend pipeline (no I/O — unit-testable).

The repo layer enforces these on every transition; keeping them here (like
detector.py) lets the safety tests run without SQLAlchemy installed.
"""

from __future__ import annotations

# Actors that may never approve/decline/publish — mirrors PlanRepo's rule that
# the orchestrator cannot self-approve (PRD §8).
_NON_HUMAN_ACTORS = {"", "system", "orchestrator", "trend_scout", "scout", "scheduler"}

_PIPELINE_TRANSITIONS: dict[str, set[str]] = {
    "pending_approval": {"approved", "declined", "expired", "closed"},
    "approved": {"generating", "closed", "failed"},
    "generating": {"previews_ready", "failed", "closed"},
    "previews_ready": {"generating", "published", "partially_published", "closed"},
    "partially_published": {"generating", "published", "closed"},
    "published": {"closed"},
    "failed": {"generating", "closed"},
    # terminal: declined, expired, closed
    "declined": set(),
    "expired": set(),
    "closed": set(),
}

_JOB_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "cancelled"},
    "running": {"preview_ready", "failed", "cancelled"},
    "preview_ready": {"approved", "rejected", "queued", "cancelled"},  # queued = regenerate
    "approved": {"published", "queued", "rejected", "cancelled"},
    "rejected": {"queued", "cancelled"},                    # allow retry after reject
    "failed": {"queued", "cancelled"},
    # terminal: published, cancelled
    "published": set(),
    "cancelled": set(),
}

_TREND_TRANSITIONS: dict[str, set[str]] = {
    "detected": {"dossier_building", "proposed", "dismissed", "expired"},
    "dossier_building": {"detected", "proposed", "dismissed", "expired"},
    "proposed": {"approved", "declined", "dismissed", "expired"},
    "approved": {"completed", "declined", "dismissed"},
    # terminal: dismissed, declined, expired, completed
    "dismissed": set(),
    "declined": set(),
    "expired": set(),
    "completed": set(),
}

PIPELINE_OPEN_STATUSES = ("pending_approval", "approved", "generating", "previews_ready",
                          "partially_published")
TREND_OPEN_STATUSES = ("detected", "dossier_building", "proposed", "approved")
CONTENT_TYPES = ("article", "social_post", "newsletter_blurb", "video_script")


class LifecycleError(RuntimeError):
    """Raised on an invalid state transition or an illegitimate actor."""


def validate_actor(actor: str) -> None:
    """Approvals/declines/publishes need a real human identity (PRD §8)."""
    if not actor or actor.strip().lower() in _NON_HUMAN_ACTORS:
        raise LifecycleError("A human actor is required; the scout cannot self-approve.")


def validate_pipeline_transition(current: str, new: str) -> None:
    if new not in _PIPELINE_TRANSITIONS.get(current, set()):
        raise LifecycleError(f"Pipeline cannot go {current!r} → {new!r}")


def validate_job_transition(current: str, new: str) -> None:
    if new not in _JOB_TRANSITIONS.get(current, set()):
        raise LifecycleError(f"Content job cannot go {current!r} → {new!r}")


def validate_trend_transition(current: str, new: str) -> None:
    if current == new:
        return
    if new not in _TREND_TRANSITIONS.get(current, set()):
        raise LifecycleError(f"Trend cannot go {current!r} → {new!r}")


def require_open_pipeline(status: str) -> None:
    """Job mutations (review/regenerate/publish) need a live parent pipeline."""
    if status not in PIPELINE_OPEN_STATUSES:
        raise LifecycleError(f"pipeline is {status!r} — reopen is not allowed")


def validate_content_types(types: list[str] | tuple[str, ...]) -> list[str]:
    cleaned = [t.strip().lower() for t in types if t and t.strip()]
    unknown = [t for t in cleaned if t not in CONTENT_TYPES]
    if unknown:
        raise LifecycleError(f"Unknown content type(s): {', '.join(unknown)}")
    if not cleaned:
        raise LifecycleError("At least one content type is required")
    return list(dict.fromkeys(cleaned))
