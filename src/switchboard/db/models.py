"""Shared-memory ORM models — the reference DDL from PRD §7.2 / §7.3.

Core tables:
  * ``memory_entry``   — the heart of the system; typed, provenance-carrying,
                         brand-scoped, TTL-bearing entries every agent reads/writes.
  * ``plan`` / ``plan_item`` — the orchestrator's daily plan + its ranked items,
                         carrying the human-approval fields the governor enforces.
  * ``trend`` / ``content_pipeline`` / ``content_job`` — the competitor-trend
                         pipeline: clustered trends, human-gated trigger requests,
                         and per-content-type generation jobs (docs/trend-pipeline.md).
  * ``tool_call_log``  — per external tool call: provenance + spend (mirrors
                         Seona's AgentUsage / HC-Viral's AgentEvent).
  * ``spend_ledger``   — rolling spend the governor's caps read from.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .enums import EntryType

# Native Postgres ENUM for memory_entry.type. create_type=True lets a bare
# Base.metadata.create_all() bootstrap it (dev/tests); the Alembic migration
# manages it explicitly with create_type=False to control ordering.
entry_type_enum = ENUM(
    EntryType,
    name="entry_type",
    create_type=True,
    values_callable=lambda e: [m.value for m in e],
)


class MemoryEntry(Base):
    __tablename__ = "memory_entry"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    type: Mapped[EntryType] = mapped_column(entry_type_enum, nullable=False)
    brand: Mapped[str] = mapped_column(Text, nullable=False)  # hotcars|carbuzz|topspeed|portfolio
    source_agent: Mapped[str] = mapped_column(Text, nullable=False)
    source_system: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    confidence: Mapped[float | None] = mapped_column(Float)
    source_urls: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active", server_default="active")

    __table_args__ = (
        Index("ix_memory_entry_brand_type_created", "brand", "type", "created_at"),
        Index("ix_memory_entry_payload_gin", "payload", postgresql_using="gin"),
        Index("ix_memory_entry_status", "status"),
        Index("ix_memory_entry_expires_at", "expires_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<MemoryEntry id={self.id} type={self.type} brand={self.brand} "
            f"agent={self.source_agent} verified={self.verified} status={self.status}>"
        )


class Plan(Base):
    __tablename__ = "plan"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_date: Mapped[date] = mapped_column(Date, nullable=False)
    brand: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft", server_default="draft")
    created_by: Mapped[str] = mapped_column(
        Text, nullable=False, default="orchestrator", server_default="orchestrator"
    )
    approved_by: Mapped[str | None] = mapped_column(Text)  # editor/admin identity (Google SSO email)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    items: Mapped[list["PlanItem"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlanItem.rank"
    )

    __table_args__ = (Index("ix_plan_date_brand", "plan_date", "brand"),)

    def __repr__(self) -> str:
        return f"<Plan id={self.id} date={self.plan_date} brand={self.brand} status={self.status}>"


class PlanItem(Base):
    __tablename__ = "plan_item"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("plan.id", ondelete="CASCADE")
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    assigned_agent: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="proposed", server_default="proposed"
    )
    # Dry-run by default (PRD §8): live writes require an approved item to flip this.
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    cost_estimate: Mapped[dict | None] = mapped_column(JSONB)  # {ahrefs_units, llm_micros, bq_bytes}
    result_ref: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    plan: Mapped[Plan | None] = relationship(back_populates="items")

    __table_args__ = (Index("ix_plan_item_plan_status", "plan_id", "status"),)

    def __repr__(self) -> str:
        return (
            f"<PlanItem id={self.id} rank={self.rank} action={self.action_type} "
            f"agent={self.assigned_agent} status={self.status} dry_run={self.dry_run}>"
        )


class Trend(Base):
    """One clustered competitor story/topic (docs/trend-pipeline.md). Raw
    signals live in ``memory_entry`` (kind='trend_signals'); this row is the
    deduped, scored, lifecycle-bearing cluster the humans act on."""

    __tablename__ = "trend"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    brand: Mapped[str] = mapped_column(Text, nullable=False)  # hotcars|carbuzz|topspeed|portfolio
    cluster_key: Mapped[str] = mapped_column(Text, nullable=False)  # stable dedupe key
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    score_breakdown: Mapped[dict | None] = mapped_column(JSONB)  # explainable factors
    velocity: Mapped[float | None] = mapped_column(Float)        # signals/hour
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    signal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    covered_by_us: Mapped[bool | None] = mapped_column(Boolean)  # None = unknown
    entities: Mapped[dict | None] = mapped_column(JSONB)         # {oems, models, terms}
    evidence: Mapped[list | None] = mapped_column(JSONB)         # [{origin, source, title, url, published_at}]
    dossier: Mapped[dict | None] = mapped_column(JSONB)          # collected research (null until built)
    dossier_ref: Mapped[dict | None] = mapped_column(JSONB)      # artifact pointer for rendered dossier
    status: Mapped[str] = mapped_column(Text, nullable=False, default="detected", server_default="detected")
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="scout", server_default="scout")  # scout|manual
    # Activity lifecycle (PRD §16.2) — distinct from `status` (the action/pipeline
    # lifecycle). Tracks emerging → rising → peak → declining → dormant, with soft,
    # forward-only auto-suppression of fading trends (never unpublishes).
    state: Mapped[str] = mapped_column(Text, nullable=False, default="emerging", server_default="emerging")
    suppressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    suppressed_by: Mapped[str | None] = mapped_column(Text)   # NULL = auto; else the human/agent that confirmed
    evergreen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    peak_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # perishability
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    pipelines: Mapped[list["ContentPipeline"]] = relationship(
        back_populates="trend", order_by="ContentPipeline.id"
    )

    __table_args__ = (
        Index("ix_trend_brand_status_score", "brand", "status", "score"),
        Index("ix_trend_cluster_key", "cluster_key"),
    )

    def __repr__(self) -> str:
        return f"<Trend id={self.id} brand={self.brand} score={self.score:.0f} status={self.status}>"


class TrendActivity(Base):
    """Daily activity sample for a tracked trend (PRD §16.2): external interest +
    on-site performance of tied articles. The series backs state detection."""

    __tablename__ = "trend_activity"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trend_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("trend.id", ondelete="CASCADE"), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    external_score: Mapped[float | None] = mapped_column(Float)     # Trends/SerpAPI interest, 0..100
    onsite_sessions: Mapped[int | None] = mapped_column(BigInteger)  # sessions of tied articles
    article_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("uq_trend_activity_day", "trend_id", "as_of", unique=True),)


class TrendArticle(Base):
    """Maps a published article URL to the trend it belongs to (PRD §16.2), so
    suppression and attribution can find tied content."""

    __tablename__ = "trend_article"

    trend_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("trend.id", ondelete="CASCADE"), primary_key=True)
    url: Mapped[str] = mapped_column(Text, primary_key=True)
    brand: Mapped[str] = mapped_column(Text, nullable=False)


class ContentPipeline(Base):
    """A trigger request: 'generate content for this trend' — pending until a
    human approves or declines. Approval fields mirror plan/plan_item (PRD §8):
    the scout can never self-approve."""

    __tablename__ = "content_pipeline"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trend_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("trend.id", ondelete="SET NULL")
    )
    brand: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending_approval", server_default="pending_approval"
    )
    requested_by: Mapped[str] = mapped_column(
        Text, nullable=False, default="trend_scout", server_default="trend_scout"
    )
    approved_by: Mapped[str | None] = mapped_column(Text)   # human identity (Google SSO email)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    declined_by: Mapped[str | None] = mapped_column(Text)
    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(Text)
    instructions: Mapped[str | None] = mapped_column(Text)  # editor guidance at approval time
    content_types: Mapped[list | None] = mapped_column(JSONB)  # requested content types
    events: Mapped[list | None] = mapped_column(JSONB)      # audit timeline [{at, actor, event, detail}]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    trend: Mapped[Trend | None] = relationship(back_populates="pipelines")
    jobs: Mapped[list["ContentJob"]] = relationship(
        back_populates="pipeline", cascade="all, delete-orphan", order_by="ContentJob.id"
    )

    __table_args__ = (Index("ix_content_pipeline_brand_status", "brand", "status"),)

    def __repr__(self) -> str:
        return f"<ContentPipeline id={self.id} brand={self.brand} status={self.status}>"


class ContentJob(Base):
    """One generator invocation (article / social post / …) inside a pipeline.
    Regeneration bumps ``attempt`` and archives the prior attempt in ``history``."""

    __tablename__ = "content_job"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("content_pipeline.id", ondelete="CASCADE")
    )
    content_type: Mapped[str] = mapped_column(Text, nullable=False)  # article|social_post|newsletter_blurb|video_script
    transport: Mapped[str] = mapped_column(Text, nullable=False, default="llm", server_default="llm")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued", server_default="queued")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    instructions: Mapped[str | None] = mapped_column(Text)  # latest regen instructions
    history: Mapped[list | None] = mapped_column(JSONB)     # prior attempts [{attempt, instructions, preview_ref, at}]
    preview_ref: Mapped[dict | None] = mapped_column(JSONB)  # artifact pointer for the preview
    preview_meta: Mapped[dict | None] = mapped_column(JSONB)  # {title, word_count, excerpt, ...}
    external_ref: Mapped[dict | None] = mapped_column(JSONB)  # e.g. {"hc_viral_topic_id": 42}
    result_ref: Mapped[dict | None] = mapped_column(JSONB)   # publish outcome
    cost: Mapped[dict | None] = mapped_column(JSONB)         # {llm_micros, usd}
    error: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(Text)    # approve/reject attribution
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pipeline: Mapped[ContentPipeline | None] = relationship(back_populates="jobs")

    __table_args__ = (Index("ix_content_job_pipeline_status", "pipeline_id", "status"),)

    def __repr__(self) -> str:
        return (
            f"<ContentJob id={self.id} type={self.content_type} transport={self.transport} "
            f"status={self.status} attempt={self.attempt}>"
        )


class ToolCallLog(Base):
    __tablename__ = "tool_call_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent: Mapped[str] = mapped_column(Text, nullable=False)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # 'read' | 'act'
    brand: Mapped[str | None] = mapped_column(Text)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False)
    request: Mapped[dict | None] = mapped_column(JSONB)  # SECRET-REDACTED before persistence
    ok: Mapped[bool | None] = mapped_column(Boolean)
    cost: Mapped[dict | None] = mapped_column(JSONB)  # {ahrefs_units, llm_micros, bq_bytes, usd}
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_tool_call_log_agent_created", "agent", "created_at"),)


class AppUser(Base):
    """A human who can sign in. Google/dev identity maps here to a role that
    gates approvals (PRD §9.1). Not a resource credential — attribution only."""

    __tablename__ = "app_user"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="viewer", server_default="viewer")
    brands: Mapped[list[str] | None] = mapped_column(ARRAY(Text))  # brand_user scope
    name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<AppUser {self.email} role={self.role}>"


class SpendLedger(Base):
    __tablename__ = "spend_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    window_date: Mapped[date] = mapped_column(Date, nullable=False)
    metric: Mapped[str] = mapped_column(Text, nullable=False)  # ahrefs_units | llm_micros | bq_bytes
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_spend_ledger_window_metric", "window_date", "metric"),)


class WriterStats(Base):
    """Per-writer performance in a window (PRD §16.3), normalized so top-writer
    ranking controls for category/intent/recency rather than raw sessions."""

    __tablename__ = "writer_stats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    brand: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_sessions: Mapped[float] = mapped_column(Float, nullable=False)      # raw avg sessions/article
    norm_score: Mapped[float] = mapped_column(Float, nullable=False)        # cohort-normalized (see writers.py)
    is_top: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("uq_writer_stats_window", "brand", "author", "window_start", "window_end", unique=True),)


class WriterStyleProfile(Base):
    """A per-brand aggregate style profile distilled from the top writers'
    corpus (PRD §16.3). Versioned; one active per brand. Style layer only —
    never a byline; the human fact-check/outline/QA gates stay mandatory."""

    __tablename__ = "writer_style_profile"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    brand: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_authors: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    features: Mapped[dict] = mapped_column(JSONB, nullable=False)          # extracted style features
    exemplar_refs: Mapped[dict | None] = mapped_column(JSONB)              # artifact pointers to exemplars
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("uq_writer_style_profile_version", "brand", "version", unique=True),)
