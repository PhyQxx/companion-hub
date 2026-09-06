"""Add home_scene table for HOME-01.

Revision ID: 0036_home_scenes
Revises: 0035_plan_cancel
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036_home_scenes"
down_revision: str | None = "0035_plan_cancel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "home_scene",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("trigger", sa.String(length=120), nullable=False),
        sa.Column("window_start", sa.String(length=5), nullable=True),
        sa.Column("window_end", sa.String(length=5), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_home_scene_user_name"),
    )
    op.create_index("ix_home_scene_user_trigger", "home_scene", ["user_id", "trigger"])


def downgrade() -> None:
    op.drop_index("ix_home_scene_user_trigger", table_name="home_scene")
    op.drop_table("home_scene")
