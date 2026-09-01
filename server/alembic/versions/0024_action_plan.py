"""Add persistent action plans and steps for Action Engine v2.

Revision ID: 0024_action_plan
Revises: 0023_device_alias_reuse
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_action_plan"
down_revision: str | None = "0023_device_alias_reuse"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "action_plan",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_hash", sa.String(length=71), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(length=160), nullable=True),
        sa.Column("reason_code", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('awaiting_confirmation','ready','executing','completed',"
            "'partially_completed','failed','cancelled','expired')",
            name="ck_action_plan_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name="uq_action_plan_user_idempotency"
        ),
    )
    op.create_index(
        "ix_action_plan_user_created", "action_plan", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_action_plan_user_status",
        "action_plan",
        ["user_id", "status", "created_at"],
    )
    op.create_table(
        "action_step",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("action_id", sa.String(length=160), nullable=False),
        sa.Column("risk", sa.String(length=4), nullable=False),
        sa.Column("confirmation_policy", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("tool_name", sa.String(length=160), nullable=False),
        sa.Column("tool_arguments", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("verification_policy", sa.String(length=32), nullable=False),
        sa.Column("verifier_id", sa.String(length=160), nullable=True),
        sa.Column("compensation_action_id", sa.String(length=160), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("reason_code", sa.String(length=160), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('awaiting_confirmation','ready','executing','completed','failed',"
            "'cancelled','skipped','unknown_outcome','expired')",
            name="ck_action_step_status",
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["action_plan.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_action_step_idempotency"),
        sa.UniqueConstraint("plan_id", "position", name="uq_action_step_plan_position"),
    )
    op.create_index(
        "ix_action_step_plan_status", "action_step", ["plan_id", "status", "position"]
    )


def downgrade() -> None:
    op.drop_index("ix_action_step_plan_status", table_name="action_step")
    op.drop_table("action_step")
    op.drop_index("ix_action_plan_user_status", table_name="action_plan")
    op.drop_index("ix_action_plan_user_created", table_name="action_plan")
    op.drop_table("action_plan")
