"""World queries expire only still-eligible goals from committed state."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_cognitive_save_guard import database as save_database
from test_delivery_sqlite_transactions import explicit_transactions
from test_job_lifecycle_fence import during_commit
from test_perception import user_id as perception_user

from app.cognition import CognitiveStore, GoalKind, GoalStatus
from app.db import CognitiveGoalRecord, Database
from app.schemas import PrivacyLevel

database = save_database
user_id = perception_user


async def expired_goal(database: Database, owner: UUID) -> UUID:
    value = await CognitiveStore(database).create_goal(
        user_id=owner,
        kind=GoalKind.USER,
        title="fixture",
        source_kind="manual",
        source_id="fixture",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    return value.id


async def test_expired_goals_are_not_returned_as_active(database: Database, user_id: UUID) -> None:
    identifier = await expired_goal(database, user_id)
    assert not await CognitiveStore(database).active_goals(user_id, now=datetime.now(UTC))
    async with database.sessions() as session:
        assert (await session.get_one(CognitiveGoalRecord, identifier)).status == "expired"


@pytest.mark.parametrize("status", ["completed", "cancelled"])
async def test_waiting_expiry_does_not_overwrite_terminal_goal(
    database: Database, user_id: UUID, status: str
) -> None:
    identifier = await expired_goal(database, user_id)

    async def follow() -> None:
        assert not await CognitiveStore(database).active_goals(user_id, now=datetime.now(UTC))

    await during_commit(
        database,
        lambda session: session.execute(
            update(CognitiveGoalRecord)
            .where(CognitiveGoalRecord.id == identifier)
            .values(status=status)
        ),
        follow,
    )
    async with database.sessions() as session:
        assert (await session.get_one(CognitiveGoalRecord, identifier)).status == status


async def test_waiting_expiry_rechecks_changed_schedule(database: Database, user_id: UUID) -> None:
    identifier = await expired_goal(database, user_id)
    future = datetime.now(UTC) + timedelta(hours=1)

    async def follow() -> None:
        rows = await CognitiveStore(database).active_goals(user_id, now=datetime.now(UTC))
        assert len(rows) == 1 and rows[0].id == identifier and rows[0].status == GoalStatus.ACTIVE

    await during_commit(
        database,
        lambda session: session.execute(
            update(CognitiveGoalRecord)
            .where(CognitiveGoalRecord.id == identifier)
            .values(expires_at=future)
        ),
        follow,
    )
    async with database.sessions() as session:
        row = await session.get_one(CognitiveGoalRecord, identifier)
        assert row.status == "active"
        assert row.expires_at is not None and row.expires_at.replace(tzinfo=UTC) >= future


async def test_waiting_expiry_rechecks_changed_privacy(database: Database, user_id: UUID) -> None:
    identifier = await expired_goal(database, user_id)

    async def write(session: AsyncSession) -> None:
        await session.execute(
            update(CognitiveGoalRecord)
            .where(CognitiveGoalRecord.id == identifier)
            .values(privacy_level="L2")
        )

    async def follow() -> None:
        assert not await CognitiveStore(database).active_goals(
            user_id, now=datetime.now(UTC), max_privacy_level=PrivacyLevel.L1
        )

    await during_commit(database, write, follow)
    async with database.sessions() as session:
        row = await session.get_one(CognitiveGoalRecord, identifier)
        assert row.status == "active" and row.privacy_level == "L2"


async def test_goal_expiry_with_explicit_sqlite_read_transactions(
    database: Database, user_id: UUID
) -> None:
    async with explicit_transactions(database):
        await test_waiting_expiry_rechecks_changed_schedule(database, user_id)


async def test_active_goal_read_without_expiry_does_not_wait_for_sqlite_writer(
    database: Database, user_id: UUID
) -> None:
    store = CognitiveStore(database)
    await store.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="fixture",
        source_kind="manual",
        source_id="fixture",
    )
    async with database.engine.connect() as writer:
        await writer.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            rows = await asyncio.wait_for(store.active_goals(user_id, now=datetime.now(UTC)), 1)
            assert len(rows) == 1
        finally:
            await writer.exec_driver_sql("ROLLBACK")
    async with database.sessions() as session:
        assert (await session.scalar(select(CognitiveGoalRecord.status))) == "active"
