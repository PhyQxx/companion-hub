"""create versioned persona storage

Revision ID: 0006_persona_versions
Revises: 0005_interaction_turn
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_persona_versions"
down_revision: str | None = "0005_interaction_turn"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "persona_version",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_from_version", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft','published','superseded')",
            name="ck_persona_version_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_persona_version_created", "persona_version", ["created_at"])
    op.create_table(
        "persona_pointer",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("current_version_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_persona_pointer_singleton"),
        sa.ForeignKeyConstraint(
            ["current_version_id"], ["persona_version.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("persona_pointer")
    op.drop_index("ix_persona_version_created", table_name="persona_version")
    op.drop_table("persona_version")
