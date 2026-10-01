"""Persist revision contract comparison reports.

Revision ID: 0059_skill_verification_report
Revises: 0058_workflow_drafts
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0059_skill_verification_report"
down_revision: str | None = "0058_workflow_drafts"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("skill_draft", sa.Column("verification_report", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("skill_draft", "verification_report")
