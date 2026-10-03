"""Account and selected evidence stay fenced through PostgreSQL acceptance."""

import os
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import make_url
from test_decision_acceptance_fence import (
    test_account_revocation_waits_for_decision_acceptance as check_account_lock,
)
from test_decision_acceptance_fence import (
    test_evidence_mutation_waits_until_accepted_decision_commits as check_evidence_lock,
)
from test_decision_acceptance_fence import (
    test_nested_caller_mutation_cannot_change_accepted_snapshot as check_snapshot,
)
from test_decision_acceptance_fence import (
    test_waiting_acceptance_rechecks_committed_evidence_changes as check_evidence_change,
)
from test_decision_acceptance_fence import (
    test_waiting_decision_rejects_account_revoked_before_lock as check_account_change,
)

from app.db import Base, create_database
from app.ids import uuid7


@pytest.mark.parametrize(
    "case",
    [
        "account_lock",
        "account_change",
        "snapshot_decision",
        "snapshot_world",
        "change_goal",
        "change_memory",
        "lock_goal",
        "lock_memory",
    ],
)
async def test_postgres_decision_acceptance(case: str) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"decision_acceptance_{uuid7().hex}"
    created = False

    def set_search_path(connection: Any, record: Any, proxy: Any) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute(f"SET search_path TO {schema}, public")
        finally:
            cursor.close()

    try:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
            created = True
        database.engine.update_execution_options(schema_translate_map={None: schema})
        event.listen(database.engine.sync_engine, "checkout", set_search_path)
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.exec_driver_sql(
                f"ALTER TABLE {schema}.memory ADD COLUMN embedding_vec vector(256)"
            )
        if case == "account_lock":
            await check_account_lock(database)
        elif case == "account_change":
            await check_account_change(database)
        elif case.startswith("snapshot_"):
            await check_snapshot(database, case.removeprefix("snapshot_"))
        elif case.startswith("change_"):
            await check_evidence_change(database, case.removeprefix("change_"))
        else:
            await check_evidence_lock(database, case.removeprefix("lock_"))
    finally:
        if event.contains(database.engine.sync_engine, "checkout", set_search_path):
            event.remove(database.engine.sync_engine, "checkout", set_search_path)
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
