"""Background cancellation serializes before reading its Root authority."""

import asyncio
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, update
from test_delivery_sqlite_transactions import explicit_transactions
from test_job_lifecycle_fence import during_commit
from test_resource_budget import seed
from test_run_cancel_fence import prepared

from app.db import AppUserRecord, JobRecord, TaskRunEventRecord, TaskRunRecord
from app.ids import uuid7
from app.runs.store import RunStore
from scripts.benchmark_storage import FixtureStorage


async def background(storage: FixtureStorage, linked: bool) -> tuple[UUID, UUID]:
    owner, root, _ = await seed(storage.database)
    async with storage.database.sessions.begin() as session:
        await session.execute(
            update(TaskRunRecord)
            .where(TaskRunRecord.id == root)
            .values(contract={"entry": "background", "criterion": "handler_completed"})
        )
        if linked:
            session.add(
                JobRecord(
                    id=root,
                    task_run_id=root,
                    kind="fixture",
                    owner=str(owner),
                    status="queued",
                    input={},
                )
            )
    return owner, root


@pytest.mark.parametrize("linked", [False, True])
async def test_explicit_sqlite_background_cancel_fences_before_reads(
    linked: bool, tmp_path: Path
) -> None:
    storage = await prepared("sqlite", tmp_path)
    try:
        owner, root = await background(storage, linked)
        async with explicit_transactions(storage.database):
            results = await asyncio.gather(
                *[
                    RunStore(storage.database).cancel_background(root, user_id=owner)
                    for _ in range(8)
                ],
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            assert sum(result is True for result in results) == 1
        async with storage.database.sessions() as session:
            row = await session.get_one(TaskRunRecord, root)
            assert row.status == "cancelled" and row.cancel_epoch == 1
            if linked:
                job = await session.get_one(JobRecord, root)
                assert job.status == "cancelled" and job.cancel_requested_at is not None
            events = list(await session.scalars(select(TaskRunEventRecord)))
            assert len(events) == 1 and events[0].kind == "run.cancelled"
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("linked", [False, True])
async def test_waiting_background_cancel_rejects_owner_change(
    backend: str, linked: bool, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root = await background(storage, linked)
        other = uuid7()
        async with storage.database.sessions.begin() as session:
            session.add(AppUserRecord(id=other, display_name="Other fixture", status="active"))

        async def rejected() -> None:
            with pytest.raises(LookupError, match="run not found"):
                await RunStore(storage.database).cancel_background(root, user_id=owner)

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
            if linked:
                job = await session.get_one(JobRecord, root)
                assert job.status == "queued" and job.cancel_requested_at is None
            assert list(await session.scalars(select(TaskRunEventRecord))) == []
    finally:
        await storage.close()
