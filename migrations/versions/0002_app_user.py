"""app_user table for RBAC (PRD §9.1)

Revision ID: 0002_app_user
Revises: 0001_initial
Create Date: 2026-07-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_app_user"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("email", sa.Text(), primary_key=True),
        sa.Column("role", sa.Text(), nullable=False, server_default=sa.text("'viewer'")),
        sa.Column("brands", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("app_user")
