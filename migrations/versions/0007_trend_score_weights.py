"""Trend score weights (§13.19): operator-tunable scoring weights.

Revision ID: 0007_trend_score_weights
Revises: 0006_expenditure
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_trend_score_weights"
down_revision = "0006_expenditure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trend_score_weight",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_trend_score_weight_key_effective", "trend_score_weight", ["key", "effective_at"])


def downgrade() -> None:
    op.drop_index("ix_trend_score_weight_key_effective", table_name="trend_score_weight")
    op.drop_table("trend_score_weight")
