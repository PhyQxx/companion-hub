"""Device grant revocation is checked while observation waits on PostgreSQL."""

import os

import pytest
from sqlalchemy.engine import make_url
from test_observation_device_guard import (
    test_inflight_device_revocation_cannot_fallback_to_another_available_device as check_device,
)

from app.db import Base, create_database
from app.ids import uuid7


@pytest.mark.parametrize("kind", ["browser", "screen"])
async def test_postgres_observation_device_revocation(kind: str) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"observation_device_{uuid7().hex}"
    created = False
    try:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
            created = True
        database.engine.update_execution_options(schema_translate_map={None: schema})
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await check_device(database, kind, "analysis", "grants")
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
