"""Committed run state and atomic event allocation on isolated PostgreSQL."""

import os

import pytest
from sqlalchemy.engine import make_url
from test_run_transaction_fence import (
    test_cached_running_identity_cannot_overwrite_committed_cancellation as check_cache,
)
from test_run_transaction_fence import (
    test_event_allocation_rolls_back_with_failed_derived_commit as check_rollback,
)
from test_run_transaction_fence import (
    test_event_metadata_uses_current_run_not_cached_or_caller_fields as check_metadata,
)
from test_run_transaction_fence import (
    test_invalid_duplicate_missing_transitions_add_no_events as check_invalid,
)
from test_run_transaction_fence import (
    test_new_run_and_multiple_events_remain_one_transaction as check_new,
)
from test_run_transaction_fence import (
    test_parallel_cached_event_writers_allocate_distinct_sequences as check_parallel,
)
from test_run_transaction_fence import (
    test_waiting_transition_keeps_the_first_terminal_commit as check_terminal,
)

from app.db import AppUserRecord, Base, create_database
from app.ids import uuid7


@pytest.mark.parametrize(
    "case",
    [
        "cache",
        "parallel",
        "metadata",
        "rollback",
        "new",
        "invalid",
        "succeeded",
        "failed",
        "cancelled",
    ],
)
async def test_postgres_run_transactions(case: str) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"run_transaction_{uuid7().hex}"
    created = False
    try:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
            created = True
        database.engine.update_execution_options(schema_translate_map={None: schema})
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        owner = uuid7()
        async with database.sessions.begin() as session:
            session.add(AppUserRecord(id=owner, display_name="Fixture", status="active"))
        if case == "cache":
            await check_cache(database, owner)
        elif case == "parallel":
            await check_parallel(database, owner)
        elif case == "metadata":
            await check_metadata(database, owner)
        elif case == "rollback":
            await check_rollback(database, owner)
        elif case == "new":
            await check_new(database, owner)
        elif case == "invalid":
            await check_invalid(database, owner)
        else:
            await check_terminal(database, owner, case)
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
