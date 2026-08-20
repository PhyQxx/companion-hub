"""add memory subject scope and fact slots

Revision ID: 0010_memory_subject_scope
Revises: 0009_embedding_vector
Create Date: 2026-08-19

Existing memories were all produced under the original "about the user"
semantics. They are therefore backfilled to user/user:self without changing
ids, sources, embeddings, lineage or deletion-ledger references.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_memory_subject_scope"
down_revision: str | None = "0009_embedding_vector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_schema() -> None:
    op.add_column(
        "memory",
        sa.Column("subject_kind", sa.String(length=16), nullable=False, server_default="user"),
    )
    op.add_column(
        "memory",
        sa.Column(
            "subject_key", sa.String(length=160), nullable=False, server_default="user:self"
        ),
    )
    op.add_column("memory", sa.Column("fact_key", sa.String(length=160), nullable=True))
    op.add_column(
        "memory",
        sa.Column(
            "origin_kind",
            sa.String(length=32),
            nullable=False,
            server_default="user_statement",
        ),
    )
    op.create_check_constraint(
        "ck_memory_subject_kind",
        "memory",
        "subject_kind IN ('user','assistant','shared')",
    )
    op.create_check_constraint(
        "ck_memory_origin_kind",
        "memory",
        "origin_kind IN ('user_statement','assistant_statement','shared_turn',"
        "'system_event','manual')",
    )
    op.create_index(
        "ix_memory_user_subject_status",
        "memory",
        ["user_id", "subject_kind", "subject_key", "status", "importance"],
    )
    op.create_index(
        "ix_memory_fact_slot",
        "memory",
        ["user_id", "subject_kind", "subject_key", "fact_key", "status"],
    )


def _add_schema_sqlite() -> None:
    # SQLite cannot ALTER TABLE ADD CONSTRAINT, so Alembic recreates the table
    # inside batch mode. 0009 is a no-op on SQLite, so no vector-only column is
    # lost during reflection/recreation.
    with op.batch_alter_table("memory") as batch:
        batch.add_column(
            sa.Column(
                "subject_kind", sa.String(length=16), nullable=False, server_default="user"
            )
        )
        batch.add_column(
            sa.Column(
                "subject_key",
                sa.String(length=160),
                nullable=False,
                server_default="user:self",
            )
        )
        batch.add_column(sa.Column("fact_key", sa.String(length=160), nullable=True))
        batch.add_column(
            sa.Column(
                "origin_kind",
                sa.String(length=32),
                nullable=False,
                server_default="user_statement",
            )
        )
        batch.create_check_constraint(
            "ck_memory_subject_kind", "subject_kind IN ('user','assistant','shared')"
        )
        batch.create_check_constraint(
            "ck_memory_origin_kind",
            "origin_kind IN ('user_statement','assistant_statement','shared_turn',"
            "'system_event','manual')",
        )
        batch.create_index(
            "ix_memory_user_subject_status",
            ["user_id", "subject_kind", "subject_key", "status", "importance"],
        )
        batch.create_index(
            "ix_memory_fact_slot",
            ["user_id", "subject_kind", "subject_key", "fact_key", "status"],
        )


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _add_schema_sqlite()
    else:
        _add_schema()

    # Existing manual entries retain their provenance instead of being
    # mislabeled as user statements. All other old rows came from the original
    # user-message extraction path and keep the user_statement default.
    op.execute(
        "UPDATE memory SET origin_kind = 'manual' "
        "WHERE EXISTS (SELECT 1 FROM memory_source "
        "WHERE memory_source.memory_id = memory.id AND memory_source.source_kind = 'manual')"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("memory") as batch:
            batch.drop_index("ix_memory_fact_slot")
            batch.drop_index("ix_memory_user_subject_status")
            batch.drop_constraint("ck_memory_origin_kind", type_="check")
            batch.drop_constraint("ck_memory_subject_kind", type_="check")
            batch.drop_column("origin_kind")
            batch.drop_column("fact_key")
            batch.drop_column("subject_key")
            batch.drop_column("subject_kind")
        return

    op.drop_index("ix_memory_fact_slot", table_name="memory")
    op.drop_index("ix_memory_user_subject_status", table_name="memory")
    op.drop_constraint("ck_memory_origin_kind", "memory", type_="check")
    op.drop_constraint("ck_memory_subject_kind", "memory", type_="check")
    op.drop_column("memory", "origin_kind")
    op.drop_column("memory", "fact_key")
    op.drop_column("memory", "subject_key")
    op.drop_column("memory", "subject_kind")
