"""Skill draft revisions and pre-approval verification results.

Revision ID: 0053_skill_draft_revisions
Revises: 0052_skill_drafts
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0053_skill_draft_revisions"
down_revision: str | None = "0052_skill_drafts"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 与 skill_id 一致不加 DB 外键：草稿引用的技能可能已删除，
    # 审批时按 skill_not_found 拒绝即可，避免 SQLite ALTER 约束限制。
    op.add_column(
        "skill_draft",
        sa.Column("target_skill_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "skill_draft",
        sa.Column("base_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "skill_draft",
        sa.Column("verify_status", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "skill_draft",
        sa.Column("verify_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "skill_draft",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("skill_draft", "verified_at")
    op.drop_column("skill_draft", "verify_reason")
    op.drop_column("skill_draft", "verify_status")
    op.drop_column("skill_draft", "base_version")
    op.drop_column("skill_draft", "target_skill_id")
