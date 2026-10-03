"""Goal acceptance uses the committed message source and active owner."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_delivery_sqlite_transactions import explicit_transactions
from test_goal_privacy import seed
from test_job_lifecycle_fence import during_commit

from app.cognition import CognitiveStore, GoalKind, GoalView
from app.db import (
    AppUserRecord,
    CognitiveGoalRecord,
    Database,
    DeletionLedgerRecord,
    MessageRecord,
)
from scripts.benchmark_storage import open_storage


@asynccontextmanager
async def storage_for(backend: str, tmp_path: Path) -> AsyncIterator[Database]:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "goals.db", url)
    try:
        yield storage.database
    finally:
        await storage.close()


async def create(database: Database, owner: UUID, message: UUID) -> GoalView:
    return await CognitiveStore(database).create_goal(
        user_id=owner,
        kind=GoalKind.USER,
        title="Synthetic goal",
        source_kind="message",
        source_id=str(message),
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_waiting_goal_inherits_the_committed_message_privacy(
    backend: str, tmp_path: Path
) -> None:
    async with storage_for(backend, tmp_path) as database:
        owner, _, message = await seed(database, "L1")
        goal = await during_commit(
            database,
            lambda session: session.execute(
                update(MessageRecord).where(MessageRecord.id == message).values(privacy_level="L2")
            ),
            lambda: create(database, owner, message),
        )
        assert goal.privacy_level == "L2"


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_waiting_goal_rejects_a_deleted_message_source(backend: str, tmp_path: Path) -> None:
    async with storage_for(backend, tmp_path) as database:
        owner, _, message = await seed(database, "L1")

        async def rejected() -> None:
            with pytest.raises(ValueError):
                await create(database, owner, message)

        await during_commit(
            database,
            lambda session: session.execute(
                delete(MessageRecord).where(MessageRecord.id == message)
            ),
            rejected,
        )
        async with database.sessions() as session:
            assert await session.scalar(select(func.count(CognitiveGoalRecord.id))) == 0


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("source", ["manual", "message"])
async def test_inactive_owner_cannot_accept_a_goal(
    backend: str, source: str, tmp_path: Path
) -> None:
    async with storage_for(backend, tmp_path) as database:
        owner, _, message = await seed(database, "L1")
        async with database.sessions.begin() as session:
            await session.execute(
                update(AppUserRecord).where(AppUserRecord.id == owner).values(status="disabled")
            )
        with pytest.raises(ValueError):
            await CognitiveStore(database).create_goal(
                user_id=owner,
                kind=GoalKind.USER,
                title="Synthetic goal",
                source_kind=source,
                source_id=str(message) if source == "message" else "fixture",
            )
        async with database.sessions() as session:
            assert await session.scalar(select(func.count(CognitiveGoalRecord.id))) == 0


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("source", ["conversation", "message"])
async def test_goal_rejects_a_canonicalized_source_tombstone(
    backend: str, source: str, tmp_path: Path
) -> None:
    async with storage_for(backend, tmp_path) as database:
        owner, conversation, message = await seed(database, "L1")
        async with database.sessions.begin() as session:
            session.add(
                DeletionLedgerRecord(
                    entity_kind="message",
                    entity_id=(conversation if source == "conversation" else message).hex.upper(),
                    deleted_ids=[],
                    requested_by=str(owner),
                )
            )
        with pytest.raises(ValueError):
            await create(database, owner, message)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_competing_goal_acceptance_deduplicates_the_same_message(
    backend: str, tmp_path: Path
) -> None:
    async with storage_for(backend, tmp_path) as database:
        owner, _, message = await seed(database, "L1")
        goals = await asyncio.gather(*(create(database, owner, message) for _ in range(12)))
        assert len({goal.id for goal in goals}) == 1


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("entity", ["source", "owner"])
async def test_accepted_goal_holds_source_and_owner_locks_until_commit(
    backend: str, entity: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with storage_for(backend, tmp_path) as database:
        owner, _, message = await seed(database, "L1")
        checked, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        scalar = AsyncSession.scalar

        async def intercepted(self: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
            result = await scalar(self, statement, *args, **kwargs)
            if statement.is_select and "cognitive_goal.source_kind" in str(statement):
                checked.set()
                await release.wait()
            return result

        monkeypatch.setattr(AsyncSession, "scalar", intercepted)

        async def writer() -> None:
            async with database.sessions.begin() as session:
                attempted.set()
                await session.execute(
                    update(MessageRecord)
                    .where(MessageRecord.id == message)
                    .values(privacy_level="L2")
                    if entity == "source"
                    else update(AppUserRecord)
                    .where(AppUserRecord.id == owner)
                    .values(status="disabled")
                )

        accepting = asyncio.create_task(create(database, owner, message))
        changing: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(checked.wait(), 3)
            changing = asyncio.create_task(writer())
            await asyncio.wait_for(attempted.wait(), 3)
            await asyncio.sleep(0.05)
            assert not changing.done(), "source/owner writer passed the acceptance transaction"
            release.set()
            goal = await asyncio.wait_for(accepting, 3)
            assert goal.privacy_level == "L1"
            await asyncio.wait_for(changing, 3)
        finally:
            release.set()
            if not accepting.done():
                accepting.cancel()
            await asyncio.gather(accepting, return_exceptions=True)
            if changing is not None:
                if not changing.done():
                    changing.cancel()
                await asyncio.gather(changing, return_exceptions=True)
        async with database.sessions() as session:
            assert await session.scalar(select(func.count(CognitiveGoalRecord.id))) == 1


async def test_goal_acceptance_starts_with_a_write_under_explicit_sqlite_transactions(
    tmp_path: Path,
) -> None:
    async with storage_for("sqlite", tmp_path) as database:
        owner, _, message = await seed(database, "L1")
        async with explicit_transactions(database):
            goals = await asyncio.gather(*(create(database, owner, message) for _ in range(12)))
        assert len({goal.id for goal in goals}) == 1
