"""Step and expiry transaction fences on an isolated PostgreSQL schema."""

import os

import pytest
from sqlalchemy.engine import make_url
from test_job_lifecycle_fence import (
    test_all_step_mutations_require_the_current_live_claim as check_revocation,
)
from test_job_lifecycle_fence import (
    test_expiry_recovery_is_bounded_and_preserves_other_pools as check_batch,
)
from test_job_lifecycle_fence import (
    test_expiry_recovery_rechecks_after_concurrent_commit as check_expiry,
)
from test_job_lifecycle_fence import (
    test_heartbeat_does_not_revive_expired_or_terminal_jobs as check_heartbeat,
)
from test_job_lifecycle_fence import (
    test_recovery_rolls_back_job_when_run_transition_fails as check_rollback,
)
from test_job_lifecycle_fence import (
    test_release_and_success_reject_revoked_claims as check_release,
)
from test_job_lifecycle_fence import (
    test_run_work_cancel_does_not_overwrite_completed_job as check_run_cancel,
)
from test_job_lifecycle_fence import (
    test_waiting_step_cannot_overwrite_completed_step as check_step,
)
from test_job_lifecycle_fence import (
    test_waiting_step_cannot_overwrite_terminal_job as check_terminal,
)

from app.db import AppUserRecord, Base, create_database
from app.ids import uuid7


@pytest.mark.parametrize(
    "case",
    [
        "start",
        "complete",
        "fail",
        "step_complete",
        "step_fail",
        "lease",
        "cancel",
        "flag",
        "worker",
        "version",
        "renewal",
        "completion",
        "reclaim",
        "heartbeat",
        "release_lease",
        "release_cancel",
        "release_version",
        "batch",
        "run_root",
        "run_child",
        "rollback",
    ],
)
async def test_postgres_job_lifecycle(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"job_lifecycle_{uuid7().hex}"
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
        if case in {"start", "complete", "fail"}:
            await check_terminal(database, owner, case)
        elif case.startswith("step_"):
            await check_step(database, owner, case.removeprefix("step_"))
        elif case in {"renewal", "completion", "reclaim"}:
            await check_expiry(database, owner, case)
        elif case == "heartbeat":
            await check_heartbeat(database, owner)
        elif case.startswith("release_"):
            await check_release(database, owner, case.removeprefix("release_"))
        elif case == "batch":
            await check_batch(database, owner)
        elif case.startswith("run_"):
            await check_run_cancel(database, owner, case == "run_root")
        elif case == "rollback":
            await check_rollback(database, owner, monkeypatch)
        else:
            await check_revocation(database, owner, case)
    finally:
        database.engine.update_execution_options(schema_translate_map=None)
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
