"""Required children and plans are serialized through the owned PostgreSQL root."""

import os

import pytest
from sqlalchemy.engine import make_url
from test_work_enrollment_fence import (
    test_enrollment_fence_blocks_run_revocation_until_derived_commit as check_fence,
)
from test_work_enrollment_fence import (
    test_existing_job_replay_after_source_cancellation_does_not_enroll_again as check_replay,
)
from test_work_enrollment_fence import (
    test_owned_cached_enrollment_cannot_accept_changed_contract as check_cache,
)
from test_work_enrollment_fence import (
    test_parallel_enrollment_keeps_every_required_child as check_parallel,
)
from test_work_enrollment_fence import test_parallel_plans_keep_each_required_plan as check_plans
from test_work_enrollment_fence import test_parallel_replays_add_one_required_job as check_replays
from test_work_enrollment_fence import (
    test_transport_only_runs_cannot_enroll_business_work as check_criterion,
)
from test_work_enrollment_fence import (
    test_waiting_enrollment_rechecks_committed_run_cancellation as check_cancellation,
)

from app.db import AppUserRecord, Base, create_database
from app.ids import uuid7


@pytest.mark.parametrize(
    "case",
    [
        "cancel_chat",
        "cancel_background",
        "parallel_chat",
        "parallel_background",
        "fence_chat",
        "fence_background",
        "cache",
        "replay",
        "replays",
        "plans",
        "model_result_returned",
        "delivery_channels_returned",
        "provider_response_returned",
    ],
)
async def test_postgres_work_enrollment(case: str) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"work_enrollment_{uuid7().hex}"
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
        if case.startswith("cancel_"):
            await check_cancellation(database, owner, case.endswith("chat"))
        elif case.startswith("parallel_"):
            await check_parallel(database, owner, case.endswith("chat"))
        elif case.startswith("fence_"):
            await check_fence(database, owner, case.endswith("chat"))
        elif case == "cache":
            await check_cache(database, owner)
        elif case == "replay":
            await check_replay(database, owner)
        elif case == "replays":
            await check_replays(database, owner)
        elif case == "plans":
            await check_plans(database, owner)
        else:
            await check_criterion(database, owner, case)
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
