"""Admin-approved username/password login for Skill API connections.

Revision ID: 0050_skill_login_auth
Revises: 0049_skill_suggestions
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0050_skill_login_auth"
down_revision: str | None = "0049_skill_suggestions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("skill_connection", sa.Column("username_ref", sa.String(132)))
    op.add_column(
        "skill_connection",
        sa.Column("allowed_auth_paths", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("skill_connection", "allowed_auth_paths")
    op.drop_column("skill_connection", "username_ref")
