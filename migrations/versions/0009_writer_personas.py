"""Writer personas (§16.3): selectable writer-replication voices + job linkage.

Revision ID: 0009_writer_personas
Revises: 0008_writer_stats_cost
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_writer_personas"
down_revision = "0008_writer_stats_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "writer_persona",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("features", postgresql.JSONB(), nullable=True),
        sa.Column("style_brief", sa.Text(), nullable=True),
        sa.Column("exemplar_refs", postgresql.JSONB(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_writer_persona_brand_enabled", "writer_persona", ["brand", "enabled"])
    op.create_index("uq_writer_persona_brand_kind_name", "writer_persona",
                    ["brand", "kind", "name"], unique=True)
    op.add_column("content_job", sa.Column("persona_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("content_job", "persona_id")
    op.drop_index("uq_writer_persona_brand_kind_name", table_name="writer_persona")
    op.drop_index("ix_writer_persona_brand_enabled", table_name="writer_persona")
    op.drop_table("writer_persona")
