"""App settings KV — admin-tunable integrations/automations config.

First use: the trend-alert outbound webhook (notifications.py). One row per
setting key, value is JSON; last write wins.

Revision ID: 0011_app_setting
Revises: 0010_brand_topic_demand
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011_app_setting"
down_revision = "0010_brand_topic_demand"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_setting",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("app_setting")
