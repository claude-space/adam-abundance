"""Enumerations backing the shared-memory schema (PRD §7.2, §7.3).

``EntryType`` is the one true Postgres ENUM (``entry_type``); the rest are kept
as plain string columns in the DDL (matching the PRD), with these classes giving
type-safe constants on the Python side.
"""

from __future__ import annotations

from enum import Enum


class EntryType(str, Enum):
    """Typed memory entries. Mirrors ``CREATE TYPE entry_type`` in the PRD."""

    METRIC = "metric"
    DECISION = "decision"
    FLAG = "flag"
    FACT = "fact"                        # verified=true only after the Research fact-gate
    CLAIM = "claim"                      # unverified assertion (default for unconfirmed)
    PLAN_ITEM = "plan_item"
    CONTEXT = "context"
    REPORT = "report"                    # rendered digest/report + artifact pointer
    DISTRIBUTION_DRAFT = "distribution_draft"  # newsletter/social draft + artifact pointer


class EntryStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class PlanStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    DISPATCHED = "dispatched"
    DONE = "done"
    CANCELLED = "cancelled"


class PlanItemStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class TrendStatus(str, Enum):
    """Lifecycle of a detected competitor trend (docs/trend-pipeline.md)."""

    DETECTED = "detected"
    DOSSIER_BUILDING = "dossier_building"
    PROPOSED = "proposed"                # a trigger request exists, awaiting a human
    APPROVED = "approved"                # at least one pipeline approved
    DECLINED = "declined"
    DISMISSED = "dismissed"              # human said "not this one" (dedup window applies)
    EXPIRED = "expired"                  # perishable — nobody acted in time
    COMPLETED = "completed"              # a pipeline ran to published/closed


class TrendState(str, Enum):
    """Activity lifecycle of a trend (PRD §16.2) — distinct from ``TrendStatus``
    (the action/pipeline lifecycle). Tracks whether interest is emerging,
    growing, at peak, fading, or gone; drives soft auto-suppression."""

    EMERGING = "emerging"
    RISING = "rising"
    PEAK = "peak"
    DECLINING = "declining"              # sustained downtrend -> auto-suppress (unless evergreen)
    DORMANT = "dormant"                  # activity at/near zero


class PipelineStatus(str, Enum):
    """Lifecycle of a content-pipeline trigger request."""

    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    GENERATING = "generating"
    PREVIEWS_READY = "previews_ready"
    PUBLISHED = "published"
    PARTIALLY_PUBLISHED = "partially_published"
    DECLINED = "declined"
    CLOSED = "closed"
    FAILED = "failed"
    EXPIRED = "expired"


class ContentJobStatus(str, Enum):
    """Lifecycle of one generator invocation inside a pipeline."""

    QUEUED = "queued"
    RUNNING = "running"
    PREVIEW_READY = "preview_ready"
    APPROVED = "approved"                # editor accepted the preview
    PUBLISHED = "published"              # gated hand-off recorded (Emaki draft / manual)
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolAction(str, Enum):
    READ = "read"
    ACT = "act"


class SpendMetric(str, Enum):
    """The three metered resources the governor hard-caps (PRD §8)."""

    AHREFS_UNITS = "ahrefs_units"
    LLM_MICROS = "llm_micros"
    BQ_BYTES = "bq_bytes"


# Brand scope values (PRD §7.2 `brand` column). Kept as TEXT in the DDL.
PORTFOLIO = "portfolio"
