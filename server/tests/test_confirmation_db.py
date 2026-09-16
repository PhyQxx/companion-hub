"""FIX-01/01B 确认预览持久化：数据库存储跨重启存活、跨实例原子认领。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.confirmation import DatabasePendingMutationStore
from app.db import AppUserRecord, Base, Database, create_database
from app.ids import uuid7


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
async def user_id(database: Database) -> UUID:
    value = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=value, display_name="Preview owner", status="active"))
    return value


async def _prepare(
    store: DatabasePendingMutationStore,
    user_id: UUID,
    *,
    kind: str = "calendar_create",
    content: dict[str, object] | None = None,
    now: datetime | None = None,
):
    return await store.prepare(
        user_id=user_id,
        turn_id=uuid7(),
        kind=kind,
        content=content or {"title": "A"},
        preview=content or {"title": "A"},
        now=now,
    )


class TestDatabasePendingMutationStore:
    async def test_previews_survive_store_restarts(self, database: Database, user_id: UUID) -> None:
        first = await _prepare(DatabasePendingMutationStore(database), user_id)
        restarted = DatabasePendingMutationStore(database)
        items = await restarted.list(user_id)
        assert [item.id for item in items] == [first.id]
        assert items[0].content == {"title": "A"}
        claimed = await restarted.claim(user_id, first.id, first.digest)
        assert claimed.status == "saving"

    async def test_claim_is_atomic_across_store_instances(
        self, database: Database, user_id: UUID
    ) -> None:
        item = await _prepare(DatabasePendingMutationStore(database), user_id)
        store_a = DatabasePendingMutationStore(database)
        store_b = DatabasePendingMutationStore(database)
        claimed = await store_a.claim(user_id, item.id, item.digest)
        with pytest.raises(ValueError, match="not_pending"):
            await store_b.claim(user_id, item.id, item.digest)
        completed = await store_a.complete(claimed, {"id": "saved"})
        assert completed.status == "completed"
        replay = await store_b.claim(user_id, item.id, item.digest)
        assert replay.status == "completed"
        assert replay.result == {"id": "saved"}

    async def test_owner_isolation_digest_and_expiry(
        self, database: Database, user_id: UUID
    ) -> None:
        store = DatabasePendingMutationStore(database)
        outsider = uuid7()
        async with database.sessions.begin() as session:
            session.add(AppUserRecord(id=outsider, display_name="Outsider", status="active"))
        item = await _prepare(store, user_id)
        with pytest.raises(LookupError):
            await store.claim(outsider, item.id, item.digest)
        with pytest.raises(ValueError, match="changed"):
            await store.claim(user_id, item.id, "0" * 64)

        expired = await _prepare(
            store, user_id, now=datetime.now(UTC) - timedelta(hours=1)
        )
        with pytest.raises(ValueError, match="expired"):
            await store.claim(user_id, expired.id, expired.digest)
        # 过期行被下一次写入修剪，不再出现在列表
        await _prepare(store, user_id, content={"title": "fresh"})
        remaining = await store.list(user_id)
        assert expired.id not in [entry.id for entry in remaining]

    async def test_same_turn_replacement_cancels_old_preview(
        self, database: Database, user_id: UUID
    ) -> None:
        store = DatabasePendingMutationStore(database)
        turn = uuid7()
        first = await store.prepare(
            user_id=user_id,
            turn_id=turn,
            kind="mail_send",
            content={"subject": "A"},
            preview={"subject": "A"},
        )
        replay = await store.prepare(
            user_id=user_id,
            turn_id=turn,
            kind="mail_send",
            content={"subject": "A"},
            preview={"subject": "A"},
        )
        assert replay.id == first.id
        replaced = await store.prepare(
            user_id=user_id,
            turn_id=turn,
            kind="mail_send",
            content={"subject": "B"},
            preview={"subject": "B"},
        )
        assert replaced.id != first.id
        statuses = {item.id: item.status for item in await store.list(user_id)}
        assert statuses[first.id] == "cancelled"
        assert statuses[replaced.id] == "pending"
        with pytest.raises(ValueError, match="not_pending"):
            await store.claim(user_id, first.id, first.digest)

    async def test_capacity_limit(self, database: Database, user_id: UUID) -> None:
        store = DatabasePendingMutationStore(database, limit=2)
        await _prepare(store, user_id, content={"title": "1"})
        await _prepare(store, user_id, content={"title": "2"})
        with pytest.raises(OverflowError):
            await _prepare(store, user_id, content={"title": "3"})

    async def test_cancel_and_mark_unknown_persist(
        self, database: Database, user_id: UUID
    ) -> None:
        store = DatabasePendingMutationStore(database)
        to_cancel = await _prepare(store, user_id, content={"title": "cancel"})
        cancelled = await store.cancel(user_id, to_cancel.id)
        assert cancelled.status == "cancelled"
        assert (await DatabasePendingMutationStore(database).list(user_id))[0].status == "cancelled"

        to_fail = await _prepare(store, user_id, content={"title": "fail"})
        claimed = await store.claim(user_id, to_fail.id, to_fail.digest)
        await store.mark_unknown(claimed)
        statuses = {item.content["title"]: item.status for item in await store.list(user_id)}
        assert statuses["fail"] == "unknown_outcome"
        assert statuses["cancel"] == "cancelled"
