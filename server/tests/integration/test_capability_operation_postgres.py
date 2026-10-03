"""Media source admission and parent quota work against isolated PostgreSQL."""

import os
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from test_capability_runs import (
    test_media_calls_share_parent_quota_and_preserve_admission_state as check_quota,
)
from test_capability_runs import (
    test_waiting_media_call_rejects_late_thread_result as check_revocation,
)

from app.db import Base, create_database
from app.ids import uuid7


@pytest.mark.parametrize("case", ["quota", "owner", "ticket"])
async def test_postgres_owned_media_operations(tmp_path: Path, case: str) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"media_operation_{uuid7().hex}"
    created = False
    try:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
            created = True
        database.engine.update_execution_options(schema_translate_map={None: schema})
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        if case == "quota":
            await check_quota(database, tmp_path)
        else:
            await check_revocation(database, tmp_path, case)
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
