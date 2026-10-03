"""Run transitions and event sequences use committed state across workers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_cognitive_save_guard import database as save_database
from test_delivery_sqlite_transactions import explicit_transactions
from test_job_lifecycle_fence import during_commit
from test_perception import user_id as perception_user

from app.db import Database, TaskRunEventRecord, TaskRunRecord
from app.ids import uuid7
from app.runs.store import append_run_event, transition_run

database = save_database
user_id = perception_user


def record(owner: UUID, *, status: str = "running") -> TaskRunRecord:
    now = datetime.now(UTC)
    return TaskRunRecord(
        id=uuid7(),
        user_id=owner,
        status=status,
        contract={},
        privacy_level="L1",
        state_version=1,
        event_seq=0,
        cancel_epoch=0,
        created_at=now,
        updated_at=now,
    )


async def root(database: Database, owner: UUID, *, status: str = "running") -> UUID:
    value = record(owner, status=status)
    async with database.sessions.begin() as session:
        session.add(value)
    return value.id


async def events(database: Database, run_id: UUID) -> list[TaskRunEventRecord]:
    async with database.sessions() as session:
        return list(
            await session.scalars(
                select(TaskRunEventRecord)
                .where(TaskRunEventRecord.run_id == run_id)
                .order_by(TaskRunEventRecord.seq)
            )
        )


async def test_cached_running_identity_cannot_overwrite_committed_cancellation(
    database: Database, user_id: UUID
) -> None:
    run_id = await root(database, user_id)
    async with database.sessions() as cached_session:
        cached = await cached_session.get_one(TaskRunRecord, run_id)
        await cached_session.commit()
        async with database.sessions.begin() as writer:
            await transition_run(writer, run_id, "cancelled")
        assert cached.status == "running"
        async with cached_session.begin():
            await transition_run(cached_session, run_id, "succeeded")
    async with database.sessions() as session:
        row = await session.get_one(TaskRunRecord, run_id)
        assert row.status == "cancelled" and row.cancel_epoch == 1
        assert row.state_version == 2 and row.event_seq == 1
    assert [value.kind for value in await events(database, run_id)] == ["run.cancelled"]


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
async def test_waiting_transition_keeps_the_first_terminal_commit(
    database: Database, user_id: UUID, status: str
) -> None:
    run_id = await root(database, user_id)

    async def writer(session: AsyncSession) -> None:
        await transition_run(session, run_id, status)
        await session.flush()

    async def follower() -> None:
        async with database.sessions.begin() as session:
            await transition_run(
                session, run_id, "cancelled" if status != "cancelled" else "failed"
            )

    await during_commit(database, writer, follower)
    async with database.sessions() as session:
        row = await session.get_one(TaskRunRecord, run_id)
        assert row.status == status and row.state_version == 2 and row.event_seq == 1
    assert [value.kind for value in await events(database, run_id)] == [f"run.{status}"]


async def test_parallel_cached_event_writers_allocate_distinct_sequences(
    database: Database, user_id: UUID
) -> None:
    run_id = await root(database, user_id)
    barrier = asyncio.Barrier(12)

    async def writer(index: int) -> None:
        async with database.sessions() as session:
            row = await session.get_one(TaskRunRecord, run_id)
            await session.commit()  # Keep the identity, release the read transaction.
            await asyncio.wait_for(barrier.wait(), 3)
            async with session.begin():
                await append_run_event(session, row, "fixture.result", payload={"index": index})

    await asyncio.gather(*(writer(index) for index in range(12)))
    persisted = await events(database, run_id)
    assert [value.seq for value in persisted] == list(range(1, 13))
    assert {value.payload["index"] for value in persisted} == set(range(12))
    async with database.sessions() as session:
        assert (await session.get_one(TaskRunRecord, run_id)).event_seq == 12


async def test_parallel_events_with_explicit_sqlite_read_transactions(
    database: Database, user_id: UUID
) -> None:
    async with explicit_transactions(database):
        await test_parallel_cached_event_writers_allocate_distinct_sequences(database, user_id)


async def test_event_metadata_uses_current_run_not_cached_or_caller_fields(
    database: Database, user_id: UUID
) -> None:
    run_id = await root(database, user_id)
    async with database.sessions() as session:
        row = await session.get_one(TaskRunRecord, run_id)
        await session.commit()
        async with database.sessions.begin() as writer:
            await writer.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == run_id)
                .values(state_version=5, privacy_level="L2")
            )
        async with session.begin():
            await append_run_event(session, row, "fixture.result", payload={"state_version": 999})
    persisted = await events(database, run_id)
    assert len(persisted) == 1
    assert persisted[0].privacy_level == "L2"
    assert persisted[0].payload["state_version"] == 5


async def test_event_allocation_rolls_back_with_failed_derived_commit(
    database: Database, user_id: UUID
) -> None:
    run_id = await root(database, user_id)
    with pytest.raises(RuntimeError, match="fixture_rollback"):
        async with database.sessions.begin() as session:
            row = await session.get_one(TaskRunRecord, run_id)
            await append_run_event(session, row, "fixture.rolled_back")
            await session.flush()
            raise RuntimeError("fixture_rollback")
    assert not await events(database, run_id)
    async with database.sessions.begin() as session:
        row = await session.get_one(TaskRunRecord, run_id)
        await append_run_event(session, row, "fixture.accepted")
    assert [value.seq for value in await events(database, run_id)] == [1]


async def test_new_run_and_multiple_events_remain_one_transaction(
    database: Database, user_id: UUID
) -> None:
    row = record(user_id, status="accepted")
    async with database.sessions.begin() as session:
        session.add(row)
        await append_run_event(session, row, "run.accepted")
        await transition_run(session, row.id, "running")
        await transition_run(session, row.id, "succeeded")
    persisted = await events(database, row.id)
    assert [value.seq for value in persisted] == [1, 2, 3]
    assert [value.payload["state_version"] for value in persisted] == [1, 2, 3]


async def test_invalid_duplicate_missing_transitions_add_no_events(
    database: Database, user_id: UUID
) -> None:
    run_id = await root(database, user_id, status="accepted")
    async with database.sessions.begin() as session:
        await transition_run(session, run_id, "succeeded")
        await transition_run(session, run_id, "accepted")
        await transition_run(session, uuid7(), "failed")
    assert not await events(database, run_id)
    async with database.sessions() as session:
        row = await session.get_one(TaskRunRecord, run_id)
        assert row.status == "accepted" and row.state_version == 1 and row.event_seq == 0
