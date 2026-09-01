"""Add task_item table for TASK-01 reminders and scheduled tasks.

Revision ID: 0026_task_reminder
Revises: 0025_action_verification
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_task_reminder"
down_revision: str | None = "0025_action_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="reminder"),
        sa.Column("title", sa.String(length=320), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trigger_type", sa.String(length=16), nullable=False),
        sa.Column("trigger_config", sa.JSON(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=True),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fire_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_delivery", sa.JSON(), nullable=True),
        sa.Column("privacy_level", sa.String(length=2), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('active','firing','done','cancelled')", name="ck_task_item_status"
        ),
        sa.CheckConstraint("kind IN ('reminder','task')", name="ck_task_item_kind"),
        sa.CheckConstraint("trigger_type IN ('time','event')", name="ck_task_item_trigger_type"),
        sa.CheckConstraint("privacy_level IN ('L0','L1')", name="ck_task_item_privacy_level"),
    )
    op.create_index(
        "ix_task_item_user_status_created",
        "task_item",
        ["user_id", "status", "created_at"],
    )
    op.create_index("ix_task_item_due", "task_item", ["status", "trigger_type", "next_fire_at"])
    op.create_index("ix_task_item_event", "task_item", ["status", "trigger_type", "event_type"])


def downgrade() -> None:
    op.drop_index("ix_task_item_event", table_name="task_item")
    op.drop_index("ix_task_item_due", table_name="task_item")
    op.drop_index("ix_task_item_user_status_created", table_name="task_item")
    op.drop_table("task_item")
