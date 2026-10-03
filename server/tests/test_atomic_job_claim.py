"""Claim eligibility is evaluated in the write, across real competing workers."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, update
from sqlalchemy.engine import Connection
from test_delivery_sqlite_transactions import explicit_transactions
from test_job_lifecycle_fence import during_commit
from test_jobs import database as job_database
from test_jobs import tmp_db as job_temporary

from app.db import Database, JobRecord
from app.jobs import JobEngine

database = job_database
tmp_db = job_temporary


async def test_specific_claim_uses_one_statement_and_does_not_steal_live_lease(
    database: Database,
) -> None:
    engine = JobEngine(database)
    job = await engine.submit("fixture", {}, resource_class="fixture")
    statements: list[str] = []

    def observe(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        multiple: bool,
    ) -> None:
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        first = await engine.claim("first", resource_class="fixture", job_id=job.id)
        assert first is not None and first.status == "admitted" and first.attempts == 1
        assert len(statements) == 1 and statements[0].lstrip().upper().startswith("UPDATE")
        assert await engine.claim("second", resource_class="fixture", job_id=job.id) is None
        assert len(statements) == 2
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    current = await engine.get(job.id)
    assert current is not None and current.lease_owner == "first" and current.attempts == 1


async def test_competing_workers_claim_distinct_jobs_without_empty_pool_backoff(
    database: Database,
) -> None:
    engine = JobEngine(database)
    jobs = [
        await engine.submit("fixture", {}, resource_class="fixture", max_attempts=1)
        for _ in range(30)
    ]
    claimed: list[UUID] = []
    for expected in (12, 12, 6):
        batch = await asyncio.gather(
            *(engine.claim(f"worker-{index}", resource_class="fixture") for index in range(12))
        )
        accepted = [result for result in batch if result is not None]
        assert len(accepted) == expected
        assert all(result.attempts == 1 for result in accepted)
        claimed.extend(result.id for result in accepted)
    assert len(set(claimed)) == 30 and set(claimed) == {job.id for job in jobs}
    assert await engine.claim("idle", resource_class="fixture") is None


async def test_competing_claims_with_explicit_sqlite_transactions(database: Database) -> None:
    async with explicit_transactions(database):
        await test_competing_workers_claim_distinct_jobs_without_empty_pool_backoff(database)


async def test_idle_claim_pool_does_not_wait_for_sqlite_writer(database: Database) -> None:
    async with database.engine.connect() as writer:
        await writer.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            assert (
                await asyncio.wait_for(
                    JobEngine(database).claim("idle", resource_class="fixture"), 1
                )
                is None
            )
        finally:
            await writer.exec_driver_sql("ROLLBACK")


@pytest.mark.parametrize("change", ["resource", "schedule", "limit"])
async def test_waiting_claim_rechecks_current_eligibility(database: Database, change: str) -> None:
    engine = JobEngine(database)
    job = await engine.submit("fixture", {}, resource_class="fixture", max_attempts=3)
    async with database.sessions.begin() as session:
        await session.execute(update(JobRecord).where(JobRecord.id == job.id).values(attempts=1))
    variants: dict[str, dict[str, Any]] = {
        "resource": {"resource_class": "another-pool"},
        "schedule": {"available_at": datetime.now(UTC) + timedelta(hours=1)},
        "limit": {"max_attempts": 1},
    }
    result = await during_commit(
        database,
        lambda session: session.execute(
            update(JobRecord).where(JobRecord.id == job.id).values(**variants[change])
        ),
        lambda: engine.claim("late", resource_class="fixture", job_id=job.id),
    )
    assert result is None
    current = await engine.get(job.id)
    assert current is not None and current.status == "queued" and current.attempts == 1
    assert current.lease_owner is None
