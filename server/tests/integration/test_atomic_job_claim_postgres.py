"""Atomic queue selection, one-statement explicit claims and live eligibility."""

import os

import pytest
from sqlalchemy.engine import make_url
from test_atomic_job_claim import (
    test_competing_workers_claim_distinct_jobs_without_empty_pool_backoff as check_workers,
)
from test_atomic_job_claim import (
    test_specific_claim_uses_one_statement_and_does_not_steal_live_lease as check_specific,
)
from test_atomic_job_claim import (
    test_waiting_claim_rechecks_current_eligibility as check_eligibility,
)

from app.db import Base, create_database
from app.ids import uuid7


@pytest.mark.parametrize("case", ["specific", "workers", "resource", "schedule", "limit"])
async def test_postgres_atomic_claim(case: str) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"atomic_claim_{uuid7().hex}"
    created = False
    try:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
            created = True
        database.engine.update_execution_options(schema_translate_map={None: schema})
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        if case == "specific":
            await check_specific(database)
        elif case == "workers":
            await check_workers(database)
        else:
            await check_eligibility(database, case)
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
