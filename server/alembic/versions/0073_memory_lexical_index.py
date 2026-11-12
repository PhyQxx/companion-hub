"""memory lexical index: sqlite FTS5 table + postgresql trigram GIN

Revision ID: 0073_memory_lexical_index
Revises: 0072_memory_lifecycle
Create Date: 2026-11-12

方案 B1（docs/记忆系统优化实施方案 §5.1）：
- SQLite：FTS5 独立表 memory_lex(grams) 存 n-gram 分词文本，rowid=memory.id；
  应用写入/删除路径同事务同步（store.add/edit/_purge_memory_ids）。
- PostgreSQL：pg_trgm GIN 索引加速 content LIKE '%token%' 预筛。
两库都只加速"找得到"，不改变排序与召回语义；ORM 元数据不含
memory_lex（快照口径不受影响）。FTS5 分词函数与
app.memory.similarity._ngrams 语义保持一致（一元+二元、去空白）。
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0073_memory_lexical_index"
down_revision: str | None = "0072_memory_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app.memory.similarity._TOKEN_SPLIT 语义一致（含全角标点）。
_TOKEN_SPLIT = re.compile(r"[\s,.;:!?，。；：！？、()\[\]（）【】\"'“”‘’]+")  # noqa: RUF001
BATCH = 500


def _grams(content: str) -> str:
    normalized = _TOKEN_SPLIT.sub(" ", content.strip().lower())
    characters = list(normalized)
    grams = characters + [
        characters[index] + characters[index + 1] for index in range(len(characters) - 1)
    ]
    return " ".join(gram for gram in grams if gram.strip())


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_memory_content_trgm"
            " ON memory USING gin (content gin_trgm_ops)"
        )
        return
    if dialect != "sqlite":
        return
    op.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_lex USING fts5(grams)")
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, content FROM memory ORDER BY id")).fetchall()
    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        bind.execute(
            sa.text("INSERT OR REPLACE INTO memory_lex(rowid, grams) VALUES (:id, :grams)"),
            [{"id": row[0], "grams": _grams(row[1] or "")} for row in chunk],
        )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_memory_content_trgm")
        # pg_trgm 扩展保留：可能被其他索引用到，且无破坏性。
        return
    if dialect == "sqlite":
        op.execute("DROP TABLE IF EXISTS memory_lex")
