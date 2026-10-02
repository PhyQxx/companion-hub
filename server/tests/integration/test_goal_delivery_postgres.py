"""Cross-worker goal delivery claim and source-first cancellation on PostgreSQL."""

import os

import pytest
from sqlalchemy.engine import make_url
from test_goal_delivery_runs import (
    test_controls_stop_inflight_and_preserve_unknown as check_stop,
)
from test_goal_delivery_runs import (
    test_duplicate_claim_has_one_dispatch_and_budget as check_duplicate,
)

from app.db import Base, create_database
from app.ids import uuid7


@pytest.mark.parametrize("case", ["duplicate", "cancel", "defer"])
async def test_postgres_goal_delivery(case: str) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"goal_delivery_{uuid7().hex}"
    created = False
    try:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
            created = True
        database.engine.update_execution_options(schema_translate_map={None: schema})
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        if case == "duplicate":
            await check_duplicate(database)
        else:
            await check_stop(database, case)
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
