# ruff: noqa: RUF001
from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.db import AppUserRecord, Base, create_database
from app.memory import (
    MemoryRetriever,
    MemorySourceKind,
    MemorySourceRef,
    MemoryStatus,
    MemoryStore,
    MemoryType,
)
from app.memory.models import MemoryCandidate
from app.schemas import PrivacyLevel

pytestmark = pytest.mark.skipif(
    os.getenv("ARIA_TEST_DATABASE_URL") is None,
    reason="ARIA_TEST_DATABASE_URL is not configured",
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


async def test_pgvector_dual_write_and_ann_recall() -> None:
    database = create_database(os.environ["ARIA_TEST_DATABASE_URL"])
    try:
        # Other integration tests rebuild the schema via create_all; restore
        # the pgvector artifacts migration 0009 would have created.
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
            await connection.exec_driver_sql(
                "ALTER TABLE memory ADD COLUMN IF NOT EXISTS embedding_vec vector(256)"
            )
            await connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_memory_embedding_vec"
                " ON memory USING hnsw (embedding_vec vector_cosine_ops)"
            )
        store = MemoryStore(database)
        assert store.vector_sql_enabled, "pgvector recall path must be active"
        async with database.sessions.begin() as session:
            session.add(AppUserRecord(id=uuid4(), display_name="pg", status="active"))
        async with database.sessions() as session:
            user_id = await session.scalar(select(AppUserRecord.id))
        target = await store.add(
            MemoryCandidate(
                type=MemoryType.PREFERENCE,
                content="用户不吃香菜，点菜要去掉",
                privacy_level=PrivacyLevel.L1,
                sources=[
                    MemorySourceRef(source_kind=MemorySourceKind.MESSAGE, source_id="m1")
                ],
            ),
            user_id=user_id,
        )
        await store.add(
            MemoryCandidate(
                type=MemoryType.SEMANTIC,
                content="用户在杭州做后端开发",
                privacy_level=PrivacyLevel.L1,
            ),
            user_id=user_id,
        )

        async def has_vector(memory_id: int) -> bool:
            async with database.sessions() as session:
                return bool(
                    await session.scalar(
                        text("SELECT embedding_vec IS NOT NULL FROM memory WHERE id = :id"),
                        {"id": memory_id},
                    )
                )

        assert await has_vector(target.id)

        result = await MemoryRetriever(store).retrieve(
            "帮我点菜，我能吃香菜吗",
            user_id=user_id,
            privacy_level=PrivacyLevel.L1,
            now=NOW,
        )
        assert result.hits
        assert result.hits[0].memory.id == target.id
        assert result.vector_recalled >= 1
        assert "vector" in result.hits[0].reasons

        replaced = await store.edit(
            target.id, content="用户不吃香菜也不能吃芹菜", actor="admin", reason="补充"
        )
        assert await has_vector(replaced.id)

        ledger_receipt = await store.hard_delete(replaced.id, actor="admin", reason="cleanup")
        assert ledger_receipt.deleted_ids
        remaining = await store.list_memories(user_id=user_id, status=MemoryStatus.ACTIVE)
        assert all(item.id not in ledger_receipt.deleted_ids for item in remaining)
    finally:
        await database.close()
