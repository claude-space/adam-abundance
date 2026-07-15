"""Trend lifecycle merge (PRD §16.2): activity-state fields on `trend` +
`trend_activity` / `trend_article` tables. Merges the competitor-trend pipeline
(status/score/evidence) with lifecycle monitoring (emerging→dormant + soft
auto-suppression) — one trend concept, not two.

Revision ID: 0004_trend_lifecycle
Revises: 0003_trend_pipeline
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_trend_lifecycle"
down_revision = "0003_trend_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- activity lifecycle on the existing trend row (the merge) --------------
    op.add_column("trend", sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'emerging'")))
    op.add_column("trend", sa.Column("suppressed", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("trend", sa.Column("suppressed_by", sa.Text(), nullable=True))
    op.add_column("trend", sa.Column("evergreen", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("trend", sa.Column("peak_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_trend_state_suppressed", "trend", ["brand", "state", "suppressed"])

    # -- daily activity series --------------------------------------------------
    op.create_table(
        "trend_activity",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trend_id", sa.BigInteger(), sa.ForeignKey("trend.id", ondelete="CASCADE"), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("external_score", sa.Float(), nullable=True),
        sa.Column("onsite_sessions", sa.BigInteger(), nullable=True),
        sa.Column("article_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("uq_trend_activity_day", "trend_activity", ["trend_id", "as_of"], unique=True)

    # -- trend ↔ published article mapping -------------------------------------
    op.create_table(
        "trend_article",
        sa.Column("trend_id", sa.BigInteger(), sa.ForeignKey("trend.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("url", sa.Text(), primary_key=True),
        sa.Column("brand", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("trend_article")
    op.drop_index("uq_trend_activity_day", table_name="trend_activity")
    op.drop_table("trend_activity")
    op.drop_index("ix_trend_state_suppressed", table_name="trend")
    op.drop_column("trend", "peak_at")
    op.drop_column("trend", "evergreen")
    op.drop_column("trend", "suppressed_by")
    op.drop_column("trend", "suppressed")
    op.drop_column("trend", "state")
