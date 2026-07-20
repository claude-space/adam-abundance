"""Per-writer cost/article on writer_stats (§16.3).

Revision ID: 0008_writer_stats_cost
Revises: 0007_trend_score_weights
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_writer_stats_cost"
down_revision = "0007_trend_score_weights"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("writer_stats", sa.Column("usd_per_article", sa.Float(), nullable=True))
    op.add_column("writer_stats", sa.Column("usd_per_word", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("writer_stats", "usd_per_word")
    op.drop_column("writer_stats", "usd_per_article")
