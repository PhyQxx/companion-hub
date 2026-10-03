"""Durable event admission under PostgreSQL worker contention and revocation."""

import os

import pytest
from sqlalchemy.engine import make_url
from test_perception_admission import (
    test_admitted_event_rejects_late_result_when_any_claim_is_lost as check_revocation,
)
from test_perception_admission import (
    test_independent_pipelines_admit_one_decision as check_contention,
)

from app.db import AppUserRecord, Base, create_database
from app.ids import uuid7


@pytest.mark.parametrize("case", ["same_event", "cross_source", "parent_cancel", "child_expiry"])
async def test_postgres_perception_admission(case: str) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"perception_admission_{uuid7().hex}"
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
        if case in {"same_event", "cross_source"}:
            await check_contention(database, owner, case)
        else:
            await check_revocation(database, owner, case)
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
