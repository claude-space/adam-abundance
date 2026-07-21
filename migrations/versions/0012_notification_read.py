"""Per-user notification read-state.

Notifications are computed on the fly from real rows (active flags, failed
content jobs, failed plan items), so "read" can't live on the item. This table
records which item KEYS a given user has marked read; the /notifications API
annotates each item's `read` from it. Composite PK (user_email, item_key) makes
marking idempotent.

Revision ID: 0012_notification_read
Revises: 0011_app_setting
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_notification_read"
down_revision = "0011_app_setting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_read",
        sa.Column("user_email", sa.Text(), primary_key=True),
        sa.Column("item_key", sa.Text(), primary_key=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("notification_read")
