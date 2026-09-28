"""Persist original SKILL.md source text on imported/created skills.

Revision ID: 0055_skill_source_markdown
Revises: 0054_skill_write_paths
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0055_skill_source_markdown"
down_revision: str | None = "0054_skill_write_paths"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("skill", sa.Column("source_markdown", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("skill", "source_markdown")
