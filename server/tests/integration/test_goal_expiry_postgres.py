"""Goal expiry does not overwrite concurrent PostgreSQL updates."""

import os

import pytest
from sqlalchemy.engine import make_url
from test_goal_expiry_transactions import (
    test_expired_goals_are_not_returned_as_active as check_expired,
)
from test_goal_expiry_transactions import (
    test_waiting_expiry_does_not_overwrite_terminal_goal as check_terminal,
)
from test_goal_expiry_transactions import (
    test_waiting_expiry_rechecks_changed_privacy as check_privacy,
)
from test_goal_expiry_transactions import (
    test_waiting_expiry_rechecks_changed_schedule as check_schedule,
)

from app.db import AppUserRecord, Base, create_database
from app.ids import uuid7


@pytest.mark.parametrize("case", ["expired", "completed", "cancelled", "privacy", "schedule"])
async def test_postgres_goal_expiry(case: str) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"goal_expiry_{uuid7().hex}"
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
        if case == "expired":
            await check_expired(database, owner)
        elif case == "privacy":
            await check_privacy(database, owner)
        elif case == "schedule":
            await check_schedule(database, owner)
        else:
            await check_terminal(database, owner, case)
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
