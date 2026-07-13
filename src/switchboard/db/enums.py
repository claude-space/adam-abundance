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
