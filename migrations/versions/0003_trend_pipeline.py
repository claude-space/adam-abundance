"""Competitor trend pipeline: trend / content_pipeline / content_job
(docs/trend-pipeline.md)

Revision ID: 0003_trend_pipeline
Revises: 0002_app_user
Create Date: 2026-07-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_trend_pipeline"
down_revision = "0002_app_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trend",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("cluster_key", sa.Text(), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("score_breakdown", postgresql.JSONB(), nullable=True),
        sa.Column("velocity", sa.Float(), nullable=True),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("signal_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("covered_by_us", sa.Boolean(), nullable=True),
        sa.Column("entities", postgresql.JSONB(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("dossier", postgresql.JSONB(), nullable=True),
        sa.Column("dossier_ref", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'detected'")),
        sa.Column("origin", sa.Text(), nullable=False, server_default=sa.text("'scout'")),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_trend_brand_status_score", "trend", ["brand", "status", "score"])
    op.create_index("ix_trend_cluster_key", "trend", ["cluster_key"])

    op.create_table(
        "content_pipeline",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trend_id", sa.BigInteger(), sa.ForeignKey("trend.id", ondelete="SET NULL"), nullable=True),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending_approval'")),
        sa.Column("requested_by", sa.Text(), nullable=False, server_default=sa.text("'trend_scout'")),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("declined_by", sa.Text(), nullable=True),
        sa.Column("declined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("content_types", postgresql.JSONB(), nullable=True),
        sa.Column("events", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_content_pipeline_brand_status", "content_pipeline", ["brand", "status"])

    op.create_table(
        "content_job",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("pipeline_id", sa.BigInteger(),
                  sa.ForeignKey("content_pipeline.id", ondelete="CASCADE"), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("transport", sa.Text(), nullable=False, server_default=sa.text("'llm'")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("history", postgresql.JSONB(), nullable=True),
        sa.Column("preview_ref", postgresql.JSONB(), nullable=True),
        sa.Column("preview_meta", postgresql.JSONB(), nullable=True),
        sa.Column("external_ref", postgresql.JSONB(), nullable=True),
        sa.Column("result_ref", postgresql.JSONB(), nullable=True),
        sa.Column("cost", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_content_job_pipeline_status", "content_job", ["pipeline_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_content_job_pipeline_status", table_name="content_job")
    op.drop_table("content_job")
    op.drop_index("ix_content_pipeline_brand_status", table_name="content_pipeline")
    op.drop_table("content_pipeline")
    op.drop_index("ix_trend_cluster_key", table_name="trend")
    op.drop_index("ix_trend_brand_status_score", table_name="trend")
    op.drop_table("trend")
