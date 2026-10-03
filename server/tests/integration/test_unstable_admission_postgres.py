"""Stability failure audits retain canonical admission and owned acceptance."""

import os

import pytest
from sqlalchemy.engine import make_url
from test_unstable_event_admission import (
    test_audit_write_rejects_owner_revoked_while_waiting_for_commit as check_owner,
)
from test_unstable_event_admission import (
    test_unstable_audit_binds_full_event_before_any_future_replay as check_binding,
)
from test_unstable_event_admission import (
    test_unstable_audit_does_not_merge_a_later_valid_distinct_event as check_valid,
)

from app.db import AppUserRecord, Base, create_database
from app.ids import uuid7


@pytest.mark.parametrize("case", ["summary", "attributes", "owner", "valid"])
async def test_postgres_unstable_admission(case: str) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"unstable_admission_{uuid7().hex}"
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
        if case in {"summary", "attributes"}:
            await check_binding(database, owner, case)
        elif case == "owner":
            await check_owner(database, owner)
        else:
            await check_valid(database, owner)
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
