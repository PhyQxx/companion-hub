"""create deletion ledger for hard deletes

Revision ID: 0008_deletion_ledger
Revises: 0007_memory_v1
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_deletion_ledger"
down_revision: str | None = "0007_memory_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deletion_ledger",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("entity_kind", sa.String(length=16), nullable=False),
        sa.Column("entity_id", sa.String(length=200), nullable=False),
        sa.Column("deleted_ids", sa.JSON(), nullable=False),
        sa.Column("requested_by", sa.String(length=160), nullable=False),
        sa.Column("reason", sa.String(length=400), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entity_kind IN ('memory','message')", name="ck_deletion_ledger_entity_kind"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_deletion_ledger_created", "deletion_ledger", ["created_at"])
    op.create_index(
        "ix_deletion_ledger_entity", "deletion_ledger", ["entity_kind", "entity_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_deletion_ledger_entity", table_name="deletion_ledger")
    op.drop_index("ix_deletion_ledger_created", table_name="deletion_ledger")
    op.drop_table("deletion_ledger")
