"""Add todo mirror metadata columns to task_item for TODO-01 pnkx sync.

Revision ID: 0031_todo_sync
Revises: 0030_calendar
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_todo_sync"
down_revision: str | None = "0030_calendar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_item") as batch:
        batch.add_column(
            sa.Column("external_updated_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("priority", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("group_label", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("task_item") as batch:
        batch.drop_column("group_label")
        batch.drop_column("priority")
        batch.drop_column("external_updated_at")
