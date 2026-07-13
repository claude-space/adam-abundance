"""initial shared-memory schema (PRD §7.2 / §7.3)

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

# The native entry_type enum. create_type=False: we create/drop it explicitly
# below so ordering is deterministic and create_table doesn't double-create it.
entry_type = postgresql.ENUM(
    "metric",
    "decision",
    "flag",
    "fact",
    "claim",
    "plan_item",
    "context",
    "report",
    "distribution_draft",
    name="entry_type",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    entry_type.create(bind, checkfirst=True)

    op.create_table(
        "memory_entry",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("type", entry_type, nullable=False),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("source_agent", sa.Text(), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_urls", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
    )
    op.create_index("ix_memory_entry_brand_type_created", "memory_entry", ["brand", "type", "created_at"])
    op.create_index("ix_memory_entry_payload_gin", "memory_entry", ["payload"], postgresql_using="gin")
    op.create_index("ix_memory_entry_status", "memory_entry", ["status"])
    op.create_index("ix_memory_entry_expires_at", "memory_entry", ["expires_at"])

    op.create_table(
        "plan",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("plan_date", sa.Date(), nullable=False),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("created_by", sa.Text(), nullable=False, server_default=sa.text("'orchestrator'")),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_plan_date_brand", "plan", ["plan_date", "brand"])

    op.create_table(
        "plan_item",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.BigInteger(), sa.ForeignKey("plan.id", ondelete="CASCADE"), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("assigned_agent", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'proposed'")),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("cost_estimate", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_plan_item_plan_status", "plan_item", ["plan_id", "status"])

    op.create_table(
        "tool_call_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("brand", sa.Text(), nullable=True),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("cost", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_tool_call_log_agent_created", "tool_call_log", ["agent", "created_at"])

    op.create_table(
        "spend_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("window_date", sa.Date(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("agent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_spend_ledger_window_metric", "spend_ledger", ["window_date", "metric"])


def downgrade() -> None:
    op.drop_table("spend_ledger")
    op.drop_table("tool_call_log")
    op.drop_table("plan_item")
    op.drop_table("plan")
    op.drop_table("memory_entry")
    entry_type.drop(op.get_bind(), checkfirst=True)
