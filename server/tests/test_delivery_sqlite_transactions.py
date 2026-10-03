"""Admission under real SQLite read transactions, beyond sqlite3 legacy mode."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection
from test_delivery_runs import database as delivery_database
from test_delivery_runs import test_concurrent_delivery_claims_before_transport as check_concurrent
from test_resource_budget import (
    test_parallel_tool_admission_is_atomic_and_settlement_is_idempotent as check_tools,
)
from test_run_budget import test_parallel_admission_cannot_overspend as check_models

from app.db import DailyBriefRecord, DailyReviewRecord, Database
from app.runs.delivery import SourceTable

database = delivery_database


@asynccontextmanager
async def explicit_transactions(database: Database) -> AsyncIterator[None]:
    # Legacy sqlite3 SELECT behavior hides read-to-write upgrade deadlocks.
    await database.engine.dispose()

    def connected(connection: Any, _: Any) -> None:
        connection.isolation_level = None

    def begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN")

    event.listen(database.engine.sync_engine, "connect", connected)
    event.listen(database.engine.sync_engine, "begin", begin)
    try:
        yield
    finally:
        event.remove(database.engine.sync_engine, "begin", begin)
        event.remove(database.engine.sync_engine, "connect", connected)


@pytest.mark.parametrize("table", [DailyBriefRecord, DailyReviewRecord])
async def test_first_write_fences_delivery_before_owner_read(
    database: Database, table: SourceTable
) -> None:
    async with explicit_transactions(database):
        await check_concurrent(database, table)


async def test_parallel_tools_do_not_upgrade_owner_reads(database: Database) -> None:
    async with explicit_transactions(database):
        await check_tools(database)


@pytest.mark.parametrize("tokens, attempts, expected", [(700, 8, 1), (10, 2, 2)])
async def test_parallel_models_do_not_upgrade_snapshot_reads(
    database: Database, tokens: int, attempts: int, expected: int
) -> None:
    async with explicit_transactions(database):
        await check_models(database, tokens, attempts, expected)
