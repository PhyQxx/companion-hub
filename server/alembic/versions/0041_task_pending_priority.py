"""TODO-01 reverse push: explicit pending_priority marker on task_item.

Revision ID: 0041_task_pending_priority
Revises: 0040_pending_mutation
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0041_task_pending_priority"
down_revision: str | None = "0040_pending_mutation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("task_item", sa.Column("pending_priority", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_item", "pending_priority")
