"""Immutable Skill versions and current version pointer.

Revision ID: 0047_skill_versions
Revises: 0046_skills
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0047_skill_versions"
down_revision: str | None = "0046_skills"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("skill", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.create_table(
        "skill_version",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "skill_id", sa.Uuid(), sa.ForeignKey("skill.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(1024), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("api_manifest", sa.JSON(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("skill_id", "version", name="uq_skill_version"),
    )
    # Existing installs from 0046 become version 1.
    op.execute(
        "INSERT INTO skill_version "
        "(id, skill_id, version, description, instructions, api_manifest, "
        "content_hash, created_at) "
        "SELECT id, id, 1, description, instructions, api_manifest, "
        "content_hash, created_at FROM skill"
    )


def downgrade() -> None:
    op.drop_table("skill_version")
    op.drop_column("skill", "version")
