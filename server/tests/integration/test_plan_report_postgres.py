"""Cross-reporter completion dispatch claims are atomic on PostgreSQL."""

import os

import pytest
from sqlalchemy.engine import make_url
from test_plan_report_outbox import test_multiple_workers_claim_one_obligation as check_outbox
from test_plan_report_runs import (
    test_same_completed_plan_has_one_report_across_reporters as check_report,
)

from app.db import Base, create_database
from app.ids import uuid7


async def test_postgres_plan_completion_report_claim() -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"plan_report_{uuid7().hex}"
    created = False
    try:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
            created = True
        database.engine.update_execution_options(schema_translate_map={None: schema})
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await check_report(database)
        await check_outbox(database)
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
