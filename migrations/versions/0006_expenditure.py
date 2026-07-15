"""Expenditure (PRD §16.4): pricing_config + writer_pay_baseline + pipeline_cost.

Revision ID: 0006_expenditure
Revises: 0005_writer_emulation
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_expenditure"
down_revision = "0005_writer_emulation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pricing_config",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=True),
        sa.Column("usd_per_unit", sa.Float(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_pricing_config_kind_key", "pricing_config", ["kind", "key"])

    op.create_table(
        "writer_pay_baseline",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("usd_per_article", sa.Float(), nullable=True),
        sa.Column("usd_per_word", sa.Float(), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "pipeline_cost",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("pipeline_run_id", sa.Text(), nullable=False),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("article_url", sa.Text(), nullable=True),
        sa.Column("action_type", sa.Text(), nullable=True),
        sa.Column("used_style_profile", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("style_profile_id", sa.BigInteger(),
                  sa.ForeignKey("writer_style_profile.id", ondelete="SET NULL"), nullable=True),
        sa.Column("cost_breakdown", postgresql.JSONB(), nullable=False),
        sa.Column("total_usd", sa.Float(), nullable=False),
        sa.Column("human_equiv_usd", sa.Float(), nullable=True),
        sa.Column("savings_usd", sa.Float(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_pipeline_cost_brand_completed", "pipeline_cost", ["brand", "completed_at"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_cost_brand_completed", table_name="pipeline_cost")
    op.drop_table("pipeline_cost")
    op.drop_table("writer_pay_baseline")
    op.drop_index("ix_pricing_config_kind_key", table_name="pricing_config")
    op.drop_table("pricing_config")
