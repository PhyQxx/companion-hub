"""Installable Skills catalog.

Revision ID: 0046_skills
Revises: 0045_theme_schedule
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0046_skills"
down_revision: str | None = "0045_theme_schedule"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "skill",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.String(1024), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("api_manifest", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("skill")
