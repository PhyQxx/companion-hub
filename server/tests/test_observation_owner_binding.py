"""Shared sources keep their original owner even after physical account deletion."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select, update
from test_cognitive_save_guard import database as save_database
from test_delivery_sqlite_transactions import explicit_transactions

from app.context.observation import ObservationOwnerGuard, ObservationUnavailable
from app.context.owners import observation_owner
from app.db import AppUserRecord, Database, ObservationOwnerBindingRecord
from app.ids import uuid7

database = save_database


async def accounts(database: Database, *, disabled: bool = False) -> tuple[UUID, UUID]:
    first, second = uuid7(), uuid7()
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add_all(
            [
                AppUserRecord(
                    id=first,
                    display_name="First",
                    status="disabled" if disabled else "active",
                    created_at=now - timedelta(days=1),
                ),
                AppUserRecord(id=second, display_name="Second", status="active", created_at=now),
            ]
        )
    return first, second


async def test_deleted_original_observation_owner_is_not_reassigned(database: Database) -> None:
    first, _ = await accounts(database)
    assert await observation_owner(database) == first
    async with database.sessions.begin() as session:
        await session.execute(delete(AppUserRecord).where(AppUserRecord.id == first))
    assert await observation_owner(database) is None
    async with database.sessions.begin() as session:
        binding = await session.get(ObservationOwnerBindingRecord, 1)
        assert binding is not None and binding.user_id == first


async def test_disabled_original_is_bound_before_activity_and_not_reassigned(
    database: Database,
) -> None:
    first, _ = await accounts(database, disabled=True)
    assert await observation_owner(database) is None
    async with database.sessions.begin() as session:
        await session.execute(delete(AppUserRecord).where(AppUserRecord.id == first))
    assert await observation_owner(database) is None


async def test_reenabled_original_owner_keeps_its_binding(database: Database) -> None:
    first, _ = await accounts(database, disabled=True)
    assert await observation_owner(database) is None
    async with database.sessions.begin() as session:
        await session.execute(
            update(AppUserRecord).where(AppUserRecord.id == first).values(status="active")
        )
    assert await observation_owner(database) == first


async def test_backdated_new_account_cannot_replace_an_established_binding(
    database: Database,
) -> None:
    first, _ = await accounts(database)
    assert await observation_owner(database) == first
    async with database.sessions.begin() as session:
        session.add(
            AppUserRecord(
                id=uuid7(),
                display_name="Backdated fixture",
                status="active",
                created_at=datetime(2000, 1, 1, tzinfo=UTC),
            )
        )
    assert await observation_owner(database) == first


async def test_initial_binding_under_concurrent_resolvers(database: Database) -> None:
    first, _ = await accounts(database)
    results = await asyncio.gather(*(observation_owner(database) for _ in range(16)))
    assert results == [first] * 16
    async with database.sessions.begin() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ObservationOwnerBindingRecord))
            == 1
        )


async def test_existing_binding_remains_read_only_under_sqlite_writer(database: Database) -> None:
    first, _ = await accounts(database)
    assert await observation_owner(database) == first
    async with database.engine.connect() as writer:
        await writer.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            assert await asyncio.wait_for(observation_owner(database), 1) == first
        finally:
            await writer.exec_driver_sql("ROLLBACK")


async def test_no_accounts_does_not_wait_for_sqlite_writer(database: Database) -> None:
    async with database.engine.connect() as writer:
        await writer.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            assert await asyncio.wait_for(observation_owner(database), 1) is None
        finally:
            await writer.exec_driver_sql("ROLLBACK")


async def test_initial_binding_with_explicit_sqlite_transactions(database: Database) -> None:
    async with explicit_transactions(database):
        await test_initial_binding_under_concurrent_resolvers(database)


async def test_inflight_owner_deletion_cannot_accept_data_for_a_successor(
    database: Database,
) -> None:
    first, _ = await accounts(database)
    assert await observation_owner(database) == first
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider() -> str:
        entered.set()
        await release.wait()
        return "private fixture result"

    guard = ObservationOwnerGuard(first, lambda: observation_owner(database))
    task = asyncio.create_task(guard.call(provider))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        async with database.sessions.begin() as session:
            await session.execute(delete(AppUserRecord).where(AppUserRecord.id == first))
        release.set()
        with pytest.raises(ObservationUnavailable):
            await task
        assert await observation_owner(database) is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
