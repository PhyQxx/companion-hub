"""Reviewable Skill drafts proposed from chat or harvested from conversations.

Revision ID: 0052_skill_drafts
Revises: 0051_skill_credentials
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0052_skill_drafts"
down_revision: str | None = "0051_skill_credentials"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "skill_draft",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("system_name", sa.String(64), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("turn_id", sa.String(64), nullable=True),
        sa.Column("dedupe_key", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_skill_draft_status_created", "skill_draft", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_skill_draft_status_created", table_name="skill_draft")
    op.drop_table("skill_draft")
