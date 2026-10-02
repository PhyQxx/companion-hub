"""Shared context ancestry and deletion predicates on PostgreSQL UUID storage."""

import os
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import make_url
from test_context_repository import (
    test_memory_ancestry_checks_source_and_unreplayed_deletions as check_ancestry,
)
from test_world_facts_repository import (
    test_feedback_cannot_link_foreign_decision_into_owned_world as check_world_facts,
)

from app.db import Base, create_database
from app.ids import uuid7


@pytest.mark.parametrize("change", ["conversation_intent", "private"])
async def test_postgres_context_ancestry(change: str) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"context_sources_{uuid7().hex}"

    def set_search_path(connection: Any, record: Any, proxy: Any) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute(f"SET search_path TO {schema}, public")
        finally:
            cursor.close()

    created = False
    try:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
            created = True
        event.listen(database.engine.sync_engine, "checkout", set_search_path)
        database.engine.update_execution_options(schema_translate_map={None: schema})
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.exec_driver_sql(
                f"ALTER TABLE {schema}.memory ADD COLUMN embedding_vec vector(256)"
            )
        await check_ancestry(database, change)
        await check_world_facts(database)
    finally:
        if event.contains(database.engine.sync_engine, "checkout", set_search_path):
            event.remove(database.engine.sync_engine, "checkout", set_search_path)
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
