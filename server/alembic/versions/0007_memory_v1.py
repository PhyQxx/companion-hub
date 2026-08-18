"""create long-term memory storage

Revision ID: 0007_memory_v1
Revises: 0006_persona_versions
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_memory_v1"
down_revision: str | None = "0006_persona_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("privacy_level", sa.String(length=2), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=True),
        sa.Column("embedding_model", sa.String(length=160), nullable=True),
        sa.Column("embedding_dimension", sa.Integer(), nullable=True),
        sa.Column("embedding_version", sa.String(length=64), nullable=True),
        sa.Column("importance", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("pin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", sa.BigInteger(), nullable=True),
        sa.Column("supersede_reason", sa.String(length=400), nullable=True),
        sa.Column("conflict_with", sa.BigInteger(), nullable=True),
        sa.Column("extractor_version", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=160), nullable=False, server_default="system"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "type IN ('episodic','semantic','preference','commitment','emotional')",
            name="ck_memory_type",
        ),
        sa.CheckConstraint("privacy_level IN ('L0','L1','L2')", name="ck_memory_privacy"),
        sa.CheckConstraint(
            "status IN ('active','archived','superseded','conflict')",
            name="ck_memory_status",
        ),
        sa.CheckConstraint(
            "importance >= 0 AND importance <= 1", name="ck_memory_importance"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["superseded_by"], ["memory.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_user_type_status", "memory", ["user_id", "type", "status", "importance"]
    )
    op.create_index("ix_memory_superseded_by", "memory", ["superseded_by"])
    op.create_index("ix_memory_created", "memory", ["created_at"])
    op.create_table(
        "memory_source",
        sa.Column("memory_id", sa.BigInteger(), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("excerpt_hash", sa.String(length=80), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_kind IN ('message','event','memory','manual')",
            name="ck_memory_source_kind",
        ),
        sa.ForeignKeyConstraint(["memory_id"], ["memory.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("memory_id", "source_kind", "source_id"),
    )
    op.create_index("ix_memory_source_lookup", "memory_source", ["source_kind", "source_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_source_lookup", table_name="memory_source")
    op.drop_table("memory_source")
    op.drop_index("ix_memory_created", table_name="memory")
    op.drop_index("ix_memory_superseded_by", table_name="memory")
    op.drop_index("ix_memory_user_type_status", table_name="memory")
    op.drop_table("memory")
