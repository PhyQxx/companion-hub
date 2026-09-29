"""DIST plan-trajectory workflow drafts.

Revision ID: 0058_workflow_drafts
Revises: 0057_conversation_summary
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0058_workflow_drafts"
down_revision: str | None = "0057_conversation_summary"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_draft",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column(
            "replay_status", sa.String(length=16), nullable=False, server_default="not_run"
        ),
        sa.Column("replay_detail", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("dedupe_key", name="uq_workflow_draft_dedupe"),
        sa.CheckConstraint(
            "status IN ('pending','approved','dismissed')", name="ck_workflow_draft_status"
        ),
        sa.CheckConstraint(
            "replay_status IN ('not_run','passed','failed','not_applicable')",
            name="ck_workflow_draft_replay",
        ),
    )
    op.create_index("ix_workflow_draft_user", "workflow_draft", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_workflow_draft_user", table_name="workflow_draft")
    op.drop_table("workflow_draft")
