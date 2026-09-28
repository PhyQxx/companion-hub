"""Encrypted, Skill-scoped login credentials.

Revision ID: 0051_skill_credentials
Revises: 0050_skill_login_auth
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0051_skill_credentials"
down_revision: str | None = "0050_skill_login_auth"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "skill_credential",
        sa.Column(
            "skill_id", sa.Uuid(), sa.ForeignKey("skill.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("connection_id", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("skill_credential")
