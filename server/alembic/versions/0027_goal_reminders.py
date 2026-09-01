"""Add goal reminder state columns for GOAL-01 commitment tracking.

Revision ID: 0027_goal_reminders
Revises: 0026_task_reminder
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_goal_reminders"
down_revision: str | None = "0026_task_reminder"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cognitive_goal") as batch:
        batch.add_column(
            sa.Column("pre_due_reminded_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("due_reminded_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("reminder_defer_until", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("ignored_count", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("cognitive_goal") as batch:
        batch.drop_column("ignored_count")
        batch.drop_column("reminder_defer_until")
        batch.drop_column("due_reminded_at")
        batch.drop_column("pre_due_reminded_at")
