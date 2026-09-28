"""Write-path allowlist for skill connections (S3 declarative writes).

Revision ID: 0054_skill_write_paths
Revises: 0053_skill_draft_revisions
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0054_skill_write_paths"
down_revision: str | None = "0053_skill_draft_revisions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 独立于读白名单：写操作只允许打到 Admin 显式声明的路径，默认为空。
    op.add_column(
        "skill_connection",
        sa.Column(
            "allowed_write_paths",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("skill_connection", "allowed_write_paths")
