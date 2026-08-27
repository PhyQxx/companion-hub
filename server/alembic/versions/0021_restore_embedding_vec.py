"""Preserve the migration chain after removing an accidental pgvector drop.

Revision ID: 0021_restore_embedding_vec
Revises: bb15882faef4
"""

from collections.abc import Sequence

revision: str = "0021_restore_embedding_vec"
down_revision: str | None = "bb15882faef4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
