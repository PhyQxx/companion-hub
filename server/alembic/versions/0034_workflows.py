"""Add workflow table for FLOW-01.

Revision ID: 0034_workflows
Revises: 0033_contacts
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034_workflows"
down_revision: str | None = "0033_contacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_workflow_user_name"),
    )
    op.create_index("ix_workflow_user", "workflow", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_workflow_user", table_name="workflow")
    op.drop_table("workflow")
