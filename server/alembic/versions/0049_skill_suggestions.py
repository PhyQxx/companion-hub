"""Evidence-backed Skill improvement suggestions.

Revision ID: 0049_skill_suggestions
Revises: 0048_skill_connections_runs
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0049_skill_suggestions"
down_revision: str | None = "0048_skill_connections_runs"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "skill_suggestion",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "skill_id", sa.Uuid(), sa.ForeignKey("skill.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("skill_version", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(50), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("guidance", sa.String(1000), nullable=False),
        sa.Column("evidence_run_ids", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_skill_suggestion_status_created", "skill_suggestion", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_skill_suggestion_status_created", table_name="skill_suggestion")
    op.drop_table("skill_suggestion")
