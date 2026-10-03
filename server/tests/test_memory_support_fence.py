"""Supporting a memory uses current owned state and serialized evidence."""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from test_delivery_sqlite_transactions import explicit_transactions
from test_job_lifecycle_fence import during_commit
from test_run_cancel_fence import prepared

from app.db import AppUserRecord, ConversationRecord, MemoryRecord, MessageRecord
from app.ids import uuid7
from app.memory import MemoryCandidate, MemoryIngester, MemorySourceRef, MemoryStore, MemoryType
from scripts.benchmark_storage import FixtureStorage


async def fixture(storage: FixtureStorage) -> tuple[UUID, UUID, int, list[MemorySourceRef]]:
    owner, other, conversation, message = uuid7(), uuid7(), uuid7(), uuid7()
    async with storage.database.sessions.begin() as session:
        session.add_all(
            [
                AppUserRecord(id=identifier, display_name="Fixture", status="active")
                for identifier in (owner, other)
            ]
        )
        await session.flush()
        session.add(ConversationRecord(id=conversation, user_id=owner, title="Fixture"))
        await session.flush()
        session.add(
            MessageRecord(
                id=message,
                conversation_id=conversation,
                turn_id=uuid7(),
                seq=1,
                role="user",
                content="Synthetic source",
                privacy_level="L1",
            )
        )
    memory = await MemoryStore(storage.database).add(
        MemoryCandidate(
            type="semantic",
            content="Synthetic fact",
            privacy_level="L1",
            importance=0.1,
            sources=[],
        ),
        user_id=owner,
    )
    return owner, other, memory.id, [MemorySourceRef(source_kind="message", source_id=str(message))]


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_owned_sources_do_not_authorize_supporting_another_users_memory(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, other, identifier, sources = await fixture(storage)
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(MemoryRecord).where(MemoryRecord.id == identifier).values(user_id=other)
            )
        store = MemoryStore(storage.database)
        with pytest.raises(LookupError, match="memory not found"):
            await store.register_support(
                identifier, sources=sources, importance_step=0.05, source_owner_id=owner
            )
        memory = await store.get(identifier)
        assert memory.user_id == other and memory.importance == 0.1 and memory.access_count == 0
        assert await store.get_sources(identifier) == []
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_candidate_support_checks_target_owner_even_without_strict_sources(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, other, identifier, sources = await fixture(storage)
        store = MemoryStore(storage.database)
        find = store.find_similar

        async def stale_similar(*args: object, **kwargs: object) -> object:
            # A synthetic DB mutation exercises the repository authority boundary;
            # the product does not offer memory transfers between accounts.
            values = await find("Synthetic fact", user_id=owner, type=MemoryType.SEMANTIC)
            async with storage.database.sessions.begin() as session:
                await session.execute(
                    update(MemoryRecord).where(MemoryRecord.id == identifier).values(user_id=other)
                )
            return values

        monkeypatch.setattr(store, "find_similar", stale_similar)
        with pytest.raises(LookupError, match="memory not found"):
            await MemoryIngester(store).ingest(
                MemoryCandidate(
                    type="semantic", content="Synthetic fact", privacy_level="L1", sources=sources
                ),
                user_id=owner,
            )
        memory = await store.get(identifier)
        assert memory.user_id == other and memory.importance == 0.1 and memory.access_count == 0
        assert await store.get_sources(identifier) == []
    finally:
        await storage.close()


async def test_explicit_sqlite_extracted_replay_creates_one_episodic_memory(tmp_path: Path) -> None:
    storage = await prepared("sqlite", tmp_path)
    try:
        owner, _, _, sources = await fixture(storage)
        store = MemoryStore(storage.database)
        value = MemoryCandidate(
            type="episodic", content="Synthetic episode", privacy_level="L1", sources=sources
        )
        async with explicit_transactions(storage.database):
            results = await asyncio.gather(
                *[store.add(value, user_id=owner, enforce_sources=True) for _ in range(8)],
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            assert (
                len({result.id for result in results if not isinstance(result, BaseException)}) == 1
            )
        memories = await store.list_memories(user_id=owner)
        assert len(memories) == 2
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_waiting_strict_support_rechecks_the_target_owner(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, other, identifier, sources = await fixture(storage)
        store = MemoryStore(storage.database)

        async def rejected() -> None:
            with pytest.raises(LookupError, match="memory not found"):
                await store.register_support(
                    identifier, sources=sources, importance_step=0.05, source_owner_id=owner
                )

        await during_commit(
            storage.database,
            lambda session: session.execute(
                update(MemoryRecord).where(MemoryRecord.id == identifier).values(user_id=other)
            ),
            rejected,
        )
        memory = await store.get(identifier)
        assert memory.user_id == other and memory.importance == 0.1 and memory.access_count == 0
        assert await store.get_sources(identifier) == []
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_support_owns_source_collection_before_guard_wait(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await prepared(backend, tmp_path)
    task = None
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        owner, _, identifier, sources = await fixture(storage)
        store = MemoryStore(storage.database)
        guard = store._guard_sources

        async def wait_guard(
            session: AsyncSession, values: Sequence[MemorySourceRef], *, user_id: UUID
        ) -> None:
            await guard(session, values, user_id=user_id)
            entered.set()
            await release.wait()

        monkeypatch.setattr(store, "_guard_sources", wait_guard)
        task = asyncio.create_task(
            store.register_support(
                identifier, sources=sources, importance_step=0.05, source_owner_id=owner
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        sources.clear()
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.importance == pytest.approx(0.15) and result.access_count == 1
        assert len(await store.get_sources(identifier)) == 1
    finally:
        release.set()
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("status", ["archived", "superseded", "conflict"])
async def test_waiting_support_cannot_strengthen_an_inactive_memory(
    backend: str,
    status: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, identifier, sources = await fixture(storage)
        store = MemoryStore(storage.database)

        async def rejected() -> None:
            with pytest.raises(ValueError, match="memory_not_active"):
                await store.register_support(
                    identifier, sources=sources, importance_step=0.05, source_owner_id=owner
                )

        await during_commit(
            storage.database,
            lambda session: session.execute(
                update(MemoryRecord).where(MemoryRecord.id == identifier).values(status=status)
            ),
            rejected,
        )
        memory = await store.get(identifier)
        assert memory.status == status and memory.importance == 0.1 and memory.access_count == 0
        assert await store.get_sources(identifier) == []
    finally:
        await storage.close()


@pytest.mark.parametrize("strict", [False, True])
async def test_explicit_sqlite_parallel_support_serializes_before_read(
    strict: bool,
    tmp_path: Path,
) -> None:
    storage = await prepared("sqlite", tmp_path)
    try:
        owner, _, identifier, sources = await fixture(storage)
        store = MemoryStore(storage.database)
        async with explicit_transactions(storage.database):
            results = await asyncio.gather(
                *[
                    store.register_support(
                        identifier,
                        sources=sources,
                        importance_step=0.05,
                        source_owner_id=owner if strict else None,
                    )
                    for _ in range(8)
                ],
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        memory = await store.get(identifier)
        expected = 1 if strict else 8
        assert memory.importance == pytest.approx(0.1 + 0.05 * expected)
        assert memory.access_count == expected
        assert len(await store.get_sources(identifier)) == 1
    finally:
        await storage.close()
