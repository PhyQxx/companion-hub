"""Add cancel_requested to action_plan for executing-time stop (PC-02).

Revision ID: 0035_plan_cancel
Revises: 0034_workflows
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_plan_cancel"
down_revision: str | None = "0034_workflows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("action_plan") as batch:
        batch.add_column(
            sa.Column(
                "cancel_requested",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("action_plan") as batch:
        batch.drop_column("cancel_requested")
