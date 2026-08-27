from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.db import Base, Database, JobStepRecord, create_database
from app.jobs import JobEngine


@pytest.fixture
def tmp_db(tmp_path: Any) -> Database:
    db = create_database(f"sqlite+aiosqlite:///{tmp_path / 'test_jobs.db'}")
    return db


@pytest.fixture
async def database(tmp_db: Database) -> Database:
    async with tmp_db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return tmp_db


@pytest.fixture
def engine(database: Database) -> JobEngine:
    return JobEngine(database)


class TestJobEngine:
    async def test_submit_and_get(self, engine: JobEngine) -> None:
        job = await engine.submit("embedding_batch", {"entity_ids": ["a", "b"]})
        assert job.kind == "embedding_batch"
        assert job.status == "queued"
        assert job.priority == 50

        fetched = await engine.get(job.id)
        assert fetched is not None
        assert fetched.id == job.id

    async def test_idempotency(self, engine: JobEngine) -> None:
        job1 = await engine.submit(
            "test", {}, idempotency_key="unique-key-001"
        )
        job2 = await engine.submit(
            "test", {}, idempotency_key="unique-key-001"
        )
        assert job1.id == job2.id

    async def test_claim(self, engine: JobEngine) -> None:
        job = await engine.submit("test", {}, priority=100)
        claimed = await engine.claim("worker-1")
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == "admitted"
        assert claimed.lease_owner == "worker-1"

    async def test_claim_no_job(self, engine: JobEngine) -> None:
        claimed = await engine.claim("worker-1")
        assert claimed is None

    async def test_claim_future_available(self, engine: JobEngine) -> None:
        future = datetime.now(UTC) + timedelta(hours=1)
        await engine.submit("test", {}, available_at=future)
        claimed = await engine.claim("worker-1")
        assert claimed is None

    async def test_renew_lease(self, engine: JobEngine) -> None:
        job = await engine.submit("test", {})
        await engine.claim("worker-1", lease_seconds=10)
        ok = await engine.renew_lease(job.id, "worker-1", lease_seconds=300)
        assert ok is True
        ok2 = await engine.renew_lease(job.id, "worker-2", lease_seconds=300)
        assert ok2 is False

    async def test_release_lease(self, engine: JobEngine) -> None:
        job = await engine.submit("test", {})
        await engine.claim("worker-1")
        ok = await engine.release_lease(job.id, "worker-1")
        assert ok is True
        after = await engine.get(job.id)
        assert after is not None
        assert after.status == "queued"

    async def test_step_lifecycle(self, engine: JobEngine) -> None:
        job = await engine.submit("test", {})
        await engine.claim("worker-1")

        step_id = await engine.start_step(job.id, "extract")
        assert step_id > 0

        await engine.complete_step(step_id, progress=0.5)
        async with engine._database.sessions() as session:
            step = await session.get(JobStepRecord, step_id)
            assert step is not None
            assert step.status == "completed"
            assert step.progress == 0.5

    async def test_fail_and_retry(self, engine: JobEngine) -> None:
        job = await engine.submit("test", {}, max_attempts=3)
        await engine.claim("worker-1")

        step_id = await engine.start_step(job.id, "extract")
        await engine.fail_step(step_id, error_code="timeout", error_detail={"detail": "slow"})

        after = await engine.get(job.id)
        assert after is not None
        assert after.status == "retry_wait"
        assert after.attempts == 1
        assert after.error_code == "timeout"

    async def test_fail_final(self, engine: JobEngine) -> None:
        job = await engine.submit("test", {}, max_attempts=1)
        await engine.claim("worker-1")

        step_id = await engine.start_step(job.id, "extract")
        await engine.fail_step(step_id, error_code="fatal")

        after = await engine.get(job.id)
        assert after is not None
        assert after.status == "failed"
        assert after.completed_at is not None

    async def test_cancel_queued(self, engine: JobEngine) -> None:
        job = await engine.submit("test", {})
        ok = await engine.cancel(job.id)
        assert ok is True
        after = await engine.get(job.id)
        assert after is not None
        assert after.status == "cancelled"
        assert after.completed_at is not None

    async def test_cancel_running(self, engine: JobEngine) -> None:
        job = await engine.submit("test", {})
        await engine.claim("worker-1")
        ok = await engine.cancel(job.id)
        assert ok is True
        after = await engine.get(job.id)
        assert after is not None
        assert after.status == "cancelling"

    async def test_confirm_cancelled(self, engine: JobEngine) -> None:
        job = await engine.submit("test", {})
        await engine.claim("worker-1")
        await engine.cancel(job.id)
        ok = await engine.confirm_cancelled(job.id, "worker-1")
        assert ok is True
        after = await engine.get(job.id)
        assert after is not None
        assert after.status == "cancelled"

    async def test_succeed(self, engine: JobEngine) -> None:
        job = await engine.submit("test", {})
        await engine.claim("worker-1")
        ok = await engine.succeed(job.id)
        assert ok is True
        after = await engine.get(job.id)
        assert after is not None
        assert after.status == "succeeded"
        assert after.progress == 1.0

    async def test_list_jobs(self, engine: JobEngine) -> None:
        await engine.submit("kind-a", {}, owner="u1")
        await engine.submit("kind-b", {}, owner="u2")
        jobs = await engine.list_jobs(owner="u1")
        assert len(jobs) == 1
        assert jobs[0].kind == "kind-a"

    async def test_priority_order(self, engine: JobEngine) -> None:
        await engine.submit("low", {}, priority=10)
        await engine.submit("high", {}, priority=90)
        claimed = await engine.claim("worker-1")
        assert claimed is not None
        assert claimed.kind == "high"

    async def test_heartbeat(self, engine: JobEngine) -> None:
        job = await engine.submit("test", {})
        await engine.claim("worker-1", lease_seconds=10)
        held = await engine.heartbeat("worker-1")
        assert len(held) == 1
        assert held[0].id == job.id

    async def test_expire_stale_leases(self, engine: JobEngine) -> None:
        job = await engine.submit("test", {}, max_attempts=1)
        await engine.claim("worker-1", lease_seconds=-1)
        count = await engine.expire_stale_leases()
        assert count == 1
        after = await engine.get(job.id)
        assert after is not None
        assert after.status == "failed"
        assert after.error_code == "lease_expired"

    async def test_expire_stale_leases_retry(self, engine: JobEngine) -> None:
        job = await engine.submit("test", {}, max_attempts=3)
        await engine.claim("worker-1", lease_seconds=-1)
        count = await engine.expire_stale_leases()
        assert count == 1
        after = await engine.get(job.id)
        assert after is not None
        assert after.status == "queued"
        assert after.lease_owner is None
