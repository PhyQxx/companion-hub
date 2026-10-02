"""Goal source identity/privacy predicates on PostgreSQL UUID storage."""

import os

import pytest
from sqlalchemy.engine import make_url
from test_goal_privacy import (
    test_message_tombstone_blocks_legacy_goal_before_restore_replay as check_tombstone,
)
from test_goal_privacy import (
    test_private_goals_do_not_enter_brief_review_or_public_world as check_world,
)

from app.db import Base, create_database
from app.ids import uuid7


async def test_postgres_goal_privacy_sources() -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"goal_privacy_{uuid7().hex}"
    created = False
    try:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
            created = True
        database.engine.update_execution_options(schema_translate_map={None: schema})
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await check_world(database)
        await check_tombstone(database)
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
