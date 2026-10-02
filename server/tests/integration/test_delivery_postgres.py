"""Transport ownership across concurrent PostgreSQL transactions."""

import os

import pytest
from sqlalchemy.engine import make_url
from test_delivery_runs import test_concurrent_delivery_claims_before_transport as check_claim

from app.db import Base, DailyBriefRecord, DailyReviewRecord, create_database
from app.ids import uuid7


async def test_postgres_delivery_claim() -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"delivery_{uuid7().hex}"
    created = False
    try:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
            created = True
        database.engine.update_execution_options(schema_translate_map={None: schema})
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        for table in (DailyBriefRecord, DailyReviewRecord):
            await check_claim(database, table)
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
