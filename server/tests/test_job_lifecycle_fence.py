"""Real transactions fence late step results, recovery and worker heartbeats."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import update
from test_claim_transaction import claim
from test_cognitive_save_guard import database as save_database
from test_perception import user_id as perception_user

from app.db import Database, JobRecord, JobStepRecord
from app.jobs.engine import JobEngine

database = save_database
user_id = perception_user


async def during_commit(
    database: Database,
    write: Callable[[Any], Awaitable[Any]],
    operation: Callable[[], Coroutine[Any, Any, Any]],
) -> Any:
    saved, release = asyncio.Event(), asyncio.Event()

    async def writer() -> None:
        async with database.sessions.begin() as session:
            await write(session)
            saved.set()
            await release.wait()

    task = asyncio.create_task(writer())
    follower: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(saved.wait(), 3)
        follower = asyncio.create_task(operation())
        await asyncio.sleep(0.1)
        release.set()
        await asyncio.wait_for(task, 3)
        return await asyncio.wait_for(follower, 3)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if follower is not None:
            if not follower.done():
                follower.cancel()
            await asyncio.gather(follower, return_exceptions=True)


@pytest.mark.parametrize("operation", ["start", "complete", "fail"])
async def test_waiting_step_cannot_overwrite_terminal_job(
    database: Database, user_id: UUID, operation: str
) -> None:
    engine, identity = await claim(database, user_id)
    step = await engine.start_step(identity.job_id, "original")

    async def follow() -> Any:
        if operation == "start":
            with pytest.raises(RuntimeError, match="job_claim_lost"):
                await engine.start_step(identity.job_id, "late")
            return None
        if operation == "complete":
            assert not await engine.complete_step(step, progress=1)
        else:
            await engine.fail_step(step, error_code="late", retryable=False)
        return None

    await during_commit(
        database,
        lambda session: session.execute(
            update(JobRecord).where(JobRecord.id == identity.job_id).values(status="succeeded")
        ),
        follow,
    )
    async with database.sessions() as session:
        job = await session.get_one(JobRecord, identity.job_id)
        original = await session.get_one(JobStepRecord, step)
        assert job.status == "succeeded" and job.error_code is None
        assert job.current_step == "original" and original.status == "running"


@pytest.mark.parametrize("operation", ["complete", "fail"])
async def test_waiting_step_cannot_overwrite_completed_step(
    database: Database, user_id: UUID, operation: str
) -> None:
    engine, identity = await claim(database, user_id)
    step = await engine.start_step(identity.job_id, "original")

    async def follow() -> Any:
        if operation == "complete":
            assert not await engine.complete_step(step, progress=0.2)
        else:
            await engine.fail_step(step, error_code="late", retryable=False)
        return None

    # Even an independent step writer without the parent lock cannot have its
    # completed result overwritten by a stale read after waiting for commit.
    await during_commit(
        database,
        lambda session: session.execute(
            update(JobStepRecord)
            .where(JobStepRecord.id == step)
            .values(status="completed", progress=1, completed_at=datetime.now(UTC))
        ),
        follow,
    )
    async with database.sessions() as session:
        job = await session.get_one(JobRecord, identity.job_id)
        original = await session.get_one(JobStepRecord, step)
        assert job.status == "admitted" and job.error_code is None
        assert original.status == "completed" and original.progress == 1


@pytest.mark.parametrize("revocation", ["lease", "cancel", "flag", "worker", "version"])
async def test_all_step_mutations_require_the_current_live_claim(
    database: Database, user_id: UUID, revocation: str
) -> None:
    engine, identity = await claim(database, user_id)
    step = await engine.start_step(identity.job_id, "original")
    variants: dict[str, dict[str, Any]] = {
        "lease": {"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        "cancel": {"status": "cancelling"},
        "flag": {"cancel_requested_at": datetime.now(UTC)},
        "worker": {"lease_owner": "another-worker"},
        "version": {"attempts": identity.version + 1},
    }
    changes = variants[revocation]
    async with database.sessions.begin() as session:
        await session.execute(
            update(JobRecord).where(JobRecord.id == identity.job_id).values(**changes)
        )
    with pytest.raises(RuntimeError, match="job_claim_lost"):
        await engine.start_step(
            identity.job_id, "late", worker_id=identity.worker_id, claim_version=identity.version
        )
    assert not await engine.complete_step(
        step, worker_id=identity.worker_id, claim_version=identity.version
    )
    await engine.fail_step(
        step,
        error_code="late",
        retryable=False,
        worker_id=identity.worker_id,
        claim_version=identity.version,
    )
    async with database.sessions() as session:
        original = await session.get_one(JobStepRecord, step)
        job = await session.get_one(JobRecord, identity.job_id)
        assert original.status == "running" and job.error_code is None
        assert job.current_step == "original"


@pytest.mark.parametrize("change", ["renewal", "completion", "reclaim"])
async def test_expiry_recovery_rechecks_after_concurrent_commit(
    database: Database, user_id: UUID, change: str
) -> None:
    engine, identity = await claim(database, user_id)
    async with database.sessions.begin() as session:
        await session.execute(
            update(JobRecord)
            .where(JobRecord.id == identity.job_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    variants: dict[str, dict[str, Any]] = {
        "renewal": {"lease_expires_at": datetime.now(UTC) + timedelta(minutes=5)},
        "completion": {"status": "succeeded"},
        "reclaim": {
            "attempts": identity.version + 1,
            "lease_owner": "replacement",
            "lease_expires_at": datetime.now(UTC) + timedelta(minutes=5),
        },
    }
    changes = variants[change]
    result = await during_commit(
        database,
        lambda session: session.execute(
            update(JobRecord).where(JobRecord.id == identity.job_id).values(**changes)
        ),
        lambda: engine.expire_stale_leases(resource_class="fixture"),
    )
    assert result == 0
    async with database.sessions() as session:
        job = await session.get_one(JobRecord, identity.job_id)
        assert job.status == ("succeeded" if change == "completion" else "admitted")
        assert job.lease_owner == ("replacement" if change == "reclaim" else identity.worker_id)
        assert job.error_code is None


async def test_heartbeat_does_not_revive_expired_or_terminal_jobs(
    database: Database, user_id: UUID
) -> None:
    engine, expired = await claim(database, user_id)
    _, terminal = await claim(database, user_id)
    _, live = await claim(database, user_id)
    expiry = datetime.now(UTC) - timedelta(seconds=1)
    async with database.sessions.begin() as session:
        await session.execute(
            update(JobRecord).where(JobRecord.id == expired.job_id).values(lease_expires_at=expiry)
        )
        await session.execute(
            update(JobRecord).where(JobRecord.id == terminal.job_id).values(status="succeeded")
        )
    held = await engine.heartbeat(expired.worker_id)
    assert [job.id for job in held] == [live.job_id]
    assert not await engine.claim_active(expired.job_id, expired.worker_id, expired.version)
    assert await engine.expire_stale_leases(resource_class="fixture") == 1


async def test_idle_expiry_pool_does_not_wait_for_sqlite_writer(database: Database) -> None:
    async with database.engine.connect() as writer:
        await writer.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            assert (
                await asyncio.wait_for(
                    JobEngine(database).expire_stale_leases(resource_class="idle"), 1
                )
                == 0
            )
        finally:
            await writer.exec_driver_sql("ROLLBACK")


@pytest.mark.parametrize("revocation", ["lease", "cancel", "version"])
async def test_release_and_success_reject_revoked_claims(
    database: Database, user_id: UUID, revocation: str
) -> None:
    engine, identity = await claim(database, user_id)
    variants: dict[str, dict[str, Any]] = {
        "lease": {"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        "cancel": {"cancel_requested_at": datetime.now(UTC)},
        "version": {"attempts": identity.version + 1},
    }
    changes = variants[revocation]
    async with database.sessions.begin() as session:
        await session.execute(
            update(JobRecord).where(JobRecord.id == identity.job_id).values(**changes)
        )
    assert not await engine.release_lease(
        identity.job_id, identity.worker_id, claim_version=identity.version
    )
    assert not await engine.succeed(
        identity.job_id, worker_id=identity.worker_id, claim_version=identity.version
    )
    if revocation != "version":
        assert not await engine.succeed(identity.job_id)
    async with database.sessions() as session:
        job = await session.get_one(JobRecord, identity.job_id)
        assert job.status == "admitted"


async def test_expiry_recovery_is_bounded_and_preserves_other_pools(
    database: Database, user_id: UUID
) -> None:
    engine = JobEngine(database)
    ids = []
    for index in range(106):
        job = await engine.submit(
            "fixture", {}, owner=str(user_id), resource_class="other" if index == 105 else "fixture"
        )
        ids.append(job.id)
    async with database.sessions.begin() as session:
        await session.execute(
            update(JobRecord)
            .where(JobRecord.id.in_(ids))
            .values(
                status="admitted",
                attempts=1,
                lease_owner="old",
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    assert await engine.expire_stale_leases(resource_class="fixture") == 100
    assert await engine.expire_stale_leases(resource_class="fixture") == 5
    assert await engine.expire_stale_leases(resource_class="fixture") == 0
    other = await engine.get(ids[-1])
    assert other is not None and other.status == "admitted" and other.lease_owner == "old"


@pytest.mark.parametrize("root_job", [False, True])
async def test_run_work_cancel_does_not_overwrite_completed_job(
    database: Database, user_id: UUID, root_job: bool
) -> None:
    from app.db import TaskRunRecord
    from app.ids import uuid7
    from app.runs.store import RunStore

    engine = JobEngine(database)
    job = await engine.submit("deleg.fixture", {}, owner=str(user_id), resource_class="fixture")
    await engine.claim("fixture-worker", resource_class="fixture", job_id=job.id)
    run_id = job.id if root_job else uuid7()
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add(
            TaskRunRecord(
                id=run_id,
                user_id=user_id,
                status="running",
                contract={"entry": "background"},
                privacy_level="L1",
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        await session.execute(
            update(JobRecord).where(JobRecord.id == job.id).values(task_run_id=run_id)
        )

    async def complete(session: Any) -> None:
        await session.execute(
            update(JobRecord).where(JobRecord.id == job.id).values(status="succeeded")
        )
        await session.execute(
            update(TaskRunRecord).where(TaskRunRecord.id == run_id).values(status="succeeded")
        )

    result = await during_commit(
        database, complete, lambda: RunStore(database).cancel_work(run_id, user_id=user_id)
    )
    assert result == (False, ())
    async with database.sessions() as session:
        finished = await session.get_one(JobRecord, job.id)
        run = await session.get_one(TaskRunRecord, run_id)
        assert finished.status == "succeeded" and finished.cancel_requested_at is None
        assert run.status == "succeeded" and not run.contract.get("work_cancel_requested")


async def test_recovery_rolls_back_job_when_run_transition_fails(
    database: Database, user_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import TaskRunRecord

    engine = JobEngine(database)
    job = await engine.submit(
        "fixture", {}, owner=str(user_id), resource_class="fixture", max_attempts=1
    )
    await engine.claim("fixture-worker", resource_class="fixture", job_id=job.id, lease_seconds=-1)
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add(
            TaskRunRecord(
                id=job.id,
                user_id=user_id,
                status="running",
                contract={"entry": "background"},
                privacy_level="L1",
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        await session.execute(
            update(JobRecord).where(JobRecord.id == job.id).values(task_run_id=job.id)
        )

    async def fail(*_: object) -> None:
        raise RuntimeError("fixture_run_transition_failed")

    monkeypatch.setattr("app.jobs.engine.transition_run", fail)
    with pytest.raises(RuntimeError, match="fixture_run_transition_failed"):
        await engine.expire_stale_leases(resource_class="fixture")
    async with database.sessions() as session:
        record = await session.get_one(JobRecord, job.id)
        run = await session.get_one(TaskRunRecord, job.id)
        assert record.status == "admitted" and record.lease_owner == "fixture-worker"
        assert record.error_code is None and record.completed_at is None
        assert run.status == "running"
