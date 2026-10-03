"""Claim locks, cached identities and terminal cancellation on PostgreSQL."""

import os

import pytest
from sqlalchemy.engine import make_url
from test_claim_transaction import (
    test_cached_job_cannot_hide_a_revoked_claim as check_cached,
)
from test_claim_transaction import (
    test_cancel_waiting_for_commit_cannot_overwrite_completed_job as check_terminal,
)
from test_claim_transaction import (
    test_claim_lock_serializes_cancellation_after_derived_commit as check_lock,
)

from app.db import AppUserRecord, Base, create_database
from app.ids import uuid7


@pytest.mark.parametrize("case", ["lock", "terminal", "cancel", "version", "lease"])
async def test_postgres_claim_transaction(case: str) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"claim_transaction_{uuid7().hex}"
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
        if case == "lock":
            await check_lock(database, owner)
        elif case == "terminal":
            await check_terminal(database, owner)
        else:
            await check_cached(database, owner, case)
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
