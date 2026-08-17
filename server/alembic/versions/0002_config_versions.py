"""create database-backed configuration versions

Revision ID: 0002_config_versions
Revises: 0001_event_outbox
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_config_versions"
down_revision: str | None = "0001_event_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONFIG_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "config_version",
        sa.Column("id", CONFIG_PK, autoincrement=True, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_from_version", CONFIG_PK, nullable=True),
        sa.CheckConstraint(
            "status IN ('draft','published','superseded')",
            name="ck_config_version_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_config_version_created", "config_version", ["created_at"])
    op.create_table(
        "config_pointer",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("current_version_id", CONFIG_PK, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="ck_config_pointer_singleton"),
        sa.ForeignKeyConstraint(
            ["current_version_id"], ["config_version.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("config_pointer")
    op.drop_index("ix_config_version_created", table_name="config_version")
    op.drop_table("config_version")
