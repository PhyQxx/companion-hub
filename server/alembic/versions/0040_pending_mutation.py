"""FIX-01/01B pending mutation previews persisted to database.

Revision ID: 0040_pending_mutation
Revises: 0039_safety_escalation
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0040_pending_mutation"
down_revision: str | None = "0039_safety_escalation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "pending_mutation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','saving','completed','unknown_outcome','cancelled')",
            name="ck_pending_mutation_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pending_mutation_user_status",
        "pending_mutation",
        ["user_id", "status"],
    )
    op.create_index(
        "ix_pending_mutation_expires",
        "pending_mutation",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_mutation_expires", table_name="pending_mutation")
    op.drop_index("ix_pending_mutation_user_status", table_name="pending_mutation")
    op.drop_table("pending_mutation")
