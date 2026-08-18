"""add pgvector column with dual-write backfill

Revision ID: 0009_embedding_vector
Revises: 0008_deletion_ledger
Create Date: 2026-08-18

PostgreSQL only: sqlite/other dialects keep the JSON embedding column and
in-process cosine recall. The vector column holds embeddings of the current
provider version only; switching embedding models requires a new column and
backfill before reads switch over (docs/02 §3.6).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_embedding_vector"
down_revision: str | None = "0008_deletion_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSION = 256


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        f"ALTER TABLE memory ADD COLUMN IF NOT EXISTS embedding_vec vector({EMBEDDING_DIMENSION})"
    )
    # jsonb::text renders "[0.1, 0.2, ...]" which is a valid vector literal.
    op.execute(
        "UPDATE memory SET embedding_vec = (embedding::text)::vector"
        " WHERE embedding IS NOT NULL AND embedding_vec IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_embedding_vec"
        " ON memory USING hnsw (embedding_vec vector_cosine_ops)"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_memory_embedding_vec")
    op.execute("ALTER TABLE memory DROP COLUMN IF EXISTS embedding_vec")
