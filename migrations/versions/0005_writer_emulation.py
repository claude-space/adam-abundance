"""Writer emulation (PRD §16.3): writer_stats + writer_style_profile.

Revision ID: 0005_writer_emulation
Revises: 0004_trend_lifecycle
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_writer_emulation"
down_revision = "0004_trend_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "writer_stats",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("article_count", sa.Integer(), nullable=False),
        sa.Column("avg_sessions", sa.Float(), nullable=False),
        sa.Column("norm_score", sa.Float(), nullable=False),
        sa.Column("is_top", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("uq_writer_stats_window", "writer_stats",
                    ["brand", "author", "window_start", "window_end"], unique=True)

    op.create_table(
        "writer_style_profile",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_authors", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("features", postgresql.JSONB(), nullable=False),
        sa.Column("exemplar_refs", postgresql.JSONB(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("uq_writer_style_profile_version", "writer_style_profile",
                    ["brand", "version"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_writer_style_profile_version", table_name="writer_style_profile")
    op.drop_table("writer_style_profile")
    op.drop_index("uq_writer_stats_window", table_name="writer_stats")
    op.drop_table("writer_stats")
