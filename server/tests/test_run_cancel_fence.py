"""Run cancellation never upgrades a stale read or cancels a changed owner."""

import asyncio
import os
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_delivery_sqlite_transactions import explicit_transactions
from test_job_lifecycle_fence import during_commit
from test_resource_budget import seed

from app.db import AppUserRecord, ConversationRecord, JobRecord, TaskRunEventRecord, TaskRunRecord
from app.db.claims import lock_job
from app.ids import uuid7
from app.runs import store
from app.runs.store import RunStore
from scripts.benchmark_storage import FixtureStorage, open_storage


async def prepared(backend: str, tmp_path: Path) -> FixtureStorage:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    return await open_storage(tmp_path / "cancel.db", url)


@pytest.mark.parametrize("sourced", [False, True])
async def test_explicit_sqlite_parallel_cancel_fences_before_read(
    sourced: bool, tmp_path: Path
) -> None:
    storage = await prepared("sqlite", tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        if sourced:
            conversation = uuid7()
            async with storage.database.sessions.begin() as session:
                session.add(ConversationRecord(id=conversation, user_id=owner, title="Fixture"))
                await session.flush()
                await session.execute(
                    update(TaskRunRecord)
                    .where(TaskRunRecord.id == root)
                    .values(conversation_id=conversation)
                )
        async with explicit_transactions(storage.database):
            results = await asyncio.gather(
                *[RunStore(storage.database).cancel_work(root, user_id=owner) for _ in range(8)],
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            assert sum(isinstance(result, tuple) and result[0] for result in results) == 1
        async with storage.database.sessions() as session:
            row = await session.get_one(TaskRunRecord, root)
            assert row.status == "cancelled" and row.cancel_epoch == 1
            events = list(await session.scalars(select(TaskRunEventRecord)))
            assert len(events) == 1 and events[0].kind == "run.cancelled"
    finally:
        await storage.close()


@pytest.mark.parametrize("sourced", [False, True])
async def test_cancel_owner_revoked_after_job_lock_rolls_back_dependent_changes(
    sourced: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await prepared("postgresql", tmp_path)
    revoked = False
    try:
        owner, root, _ = await seed(storage.database)
        other, delegate = uuid7(), uuid7()
        async with storage.database.sessions.begin() as session:
            session.add(AppUserRecord(id=other, display_name="Other fixture", status="active"))
            session.add(
                JobRecord(
                    id=delegate,
                    task_run_id=root,
                    kind="deleg.fixture",
                    owner=str(owner),
                    status="queued",
                    input={},
                )
            )
            if sourced:
                conversation = uuid7()
                session.add(ConversationRecord(id=conversation, user_id=owner, title="Fixture"))
                await session.flush()
                await session.execute(
                    update(TaskRunRecord)
                    .where(TaskRunRecord.id == root)
                    .values(conversation_id=conversation)
                )

        async def fenced_job(session: AsyncSession, identifier: UUID) -> JobRecord | None:
            nonlocal revoked
            row = await lock_job(session, identifier)
            if identifier == delegate and not revoked:
                revoked = True
                async with storage.database.sessions.begin() as writer:
                    await writer.execute(
                        update(TaskRunRecord).where(TaskRunRecord.id == root).values(user_id=other)
                    )
            return row

        monkeypatch.setattr(store, "lock_job", fenced_job)
        with pytest.raises(LookupError, match="run not found"):
            await RunStore(storage.database).cancel_work(root, user_id=owner)
        assert revoked
        async with storage.database.sessions() as session:
            row = await session.get_one(TaskRunRecord, root)
            job = await session.get_one(JobRecord, delegate)
            assert row.user_id == other and row.status == "running" and row.cancel_epoch == 0
            assert job.status == "queued" and job.cancel_requested_at is None
            assert list(await session.scalars(select(TaskRunEventRecord))) == []
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("sourced", [False, True])
async def test_waiting_cancel_cannot_mutate_a_run_after_owner_changes(
    backend: str, sourced: bool, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        other = uuid7()
        async with storage.database.sessions.begin() as session:
            session.add(AppUserRecord(id=other, display_name="Other fixture", status="active"))
            if sourced:
                conversation = uuid7()
                session.add(ConversationRecord(id=conversation, user_id=owner, title="Fixture"))
                await session.flush()
                await session.execute(
                    update(TaskRunRecord)
                    .where(TaskRunRecord.id == root)
                    .values(conversation_id=conversation)
                )

        async def rejected() -> None:
            with pytest.raises(LookupError, match="run not found"):
                await RunStore(storage.database).cancel_work(root, user_id=owner)

        await during_commit(
            storage.database,
            lambda session: session.execute(
                update(TaskRunRecord).where(TaskRunRecord.id == root).values(user_id=other)
            ),
            rejected,
        )
        async with storage.database.sessions() as session:
            row = await session.get_one(TaskRunRecord, root)
            assert row.user_id == other and row.status == "running" and row.cancel_epoch == 0
            assert not row.contract.get("work_cancel_requested")
            assert list(await session.scalars(select(TaskRunEventRecord))) == []
    finally:
        await storage.close()
