"""Per-brand topic demand (§16.3): category performance from Article Analysis.

Revision ID: 0010_brand_topic_demand
Revises: 0009_writer_personas
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_brand_topic_demand"
down_revision = "0009_writer_personas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brand_topic_demand",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("articles", sa.Integer(), nullable=False),
        sa.Column("avg_sessions", sa.Float(), nullable=False),
        sa.Column("avg_rpm", sa.Float(), nullable=True),
        sa.Column("demand_index", sa.Float(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_brand_topic_demand_brand_rank", "brand_topic_demand", ["brand", "rank"])


def downgrade() -> None:
    op.drop_index("ix_brand_topic_demand_brand_rank", table_name="brand_topic_demand")
    op.drop_table("brand_topic_demand")
