"""Add built-in UI themes and account appearance preferences.

Revision ID: 0022_ui_theme
Revises: 0021_restore_embedding_vec
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_ui_theme"
down_revision: str | None = "0021_restore_embedding_vec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ui_theme",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="published", nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("built_in", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('published','archived')", name="ck_ui_theme_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_ui_theme_key"),
    )
    op.create_table(
        "ui_preference",
        sa.Column("owner", sa.String(length=160), nullable=False),
        sa.Column("theme_id", sa.Uuid(), nullable=False),
        sa.Column("appearance_mode", sa.String(length=16), server_default="light", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "appearance_mode IN ('light','dark','system')", name="ck_ui_preference_mode"
        ),
        sa.ForeignKeyConstraint(["theme_id"], ["ui_theme.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("owner"),
    )


def downgrade() -> None:
    op.drop_table("ui_preference")
    op.drop_table("ui_theme")
