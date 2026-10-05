"""Source deletion erases job payloads while retaining content-free step audit."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_domain_delete_fence import storage_mode
from test_job_lifecycle_fence import during_commit
from test_resource_budget import seed

from app.db import ConversationRecord, Database, JobRecord, JobStepRecord, TaskRunRecord
from app.db.deletions import purge_conversation
from app.ids import uuid7
from app.jobs.engine import JobEngine


async def source(database: Database) -> tuple[UUID, UUID, UUID, UUID]:
    owner, root, _ = await seed(database)
    conversation, job, independent = uuid7(), uuid7(), uuid7()
    now = datetime.now(UTC)
    async with database.sessions.begin() as sql:
        sql.add(ConversationRecord(id=conversation, user_id=owner))
        await sql.flush()
        await sql.execute(
            update(TaskRunRecord)
            .where(TaskRunRecord.id == root)
            .values(conversation_id=conversation)
        )
        for identifier, parent in ((job, root), (independent, None)):
            sql.add(
                JobRecord(
                    id=identifier,
                    owner=str(owner),
                    task_run_id=parent,
                    kind="deleg.fixture",
                    status="running",
                    input={"private": "synthetic secret"},
                    current_step="synthetic secret",
                    error_code="synthetic secret",
                    error_detail_safe={"private": "synthetic secret"},
                    attempts=1,
                    lease_owner="fixture",
                    lease_expires_at=now + timedelta(minutes=10),
                )
            )
            await sql.flush()
            for name, state in (("synthetic secret", "running"), ("private earlier", "completed")):
                sql.add(
                    JobStepRecord(
                        job_id=identifier,
                        name=name,
                        attempt=1,
                        status=state,
                        checkpoint={"private": "synthetic secret"},
                        progress=0.5,
                        started_at=now,
                        completed_at=now if state == "completed" else None,
                    )
                )
    return owner, conversation, job, independent


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
@pytest.mark.parametrize(
    "state",
    [
        "queued",
        "admitted",
        "running",
        "retry_wait",
        "waiting_user",
        "succeeded",
        "failed",
        "cancelled",
    ],
)
async def test_erasure_preserves_audit_but_removes_all_step_payloads(
    backend: str, state: str, tmp_path: Path
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        db = storage.database
        owner, conversation, job, independent = await source(db)
        async with db.sessions.begin() as sql:
            await sql.execute(update(JobRecord).where(JobRecord.id == job).values(status=state))
        async with db.sessions() as sql:
            old_steps = {
                row.id: (row.status, row.progress, row.started_at, row.completed_at)
                for row in await sql.scalars(
                    select(JobStepRecord).where(JobStepRecord.job_id == job)
                )
            }
        async with db.sessions.begin() as sql:
            assert await purge_conversation(sql, conversation, user_id=owner)
        async with db.sessions() as sql:
            saved = await sql.get_one(JobRecord, job)
            assert saved.input == {"source_deleted": True} and saved.task_run_id is None
            assert (
                saved.current_step is None
                and saved.error_code is None
                and saved.error_detail_safe is None
            )
            steps = list(
                await sql.scalars(select(JobStepRecord).where(JobStepRecord.job_id == job))
            )
            assert len(steps) == 2
            assert len({row.name for row in steps}) == 2
            for row in steps:
                assert row.name.startswith("source_deleted:") and row.checkpoint is None
                assert (row.status, row.progress, row.started_at, row.completed_at) == old_steps[
                    row.id
                ]
            retained = await sql.get_one(JobRecord, independent)
            assert retained.input == {"private": "synthetic secret"}
            assert retained.error_detail_safe == {"private": "synthetic secret"}
            assert all(
                row.checkpoint == {"private": "synthetic secret"}
                for row in await sql.scalars(
                    select(JobStepRecord).where(JobStepRecord.job_id == independent)
                )
            )


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
@pytest.mark.parametrize("operation", ["start", "complete", "fail"])
async def test_waiting_worker_cannot_restore_erased_step_payloads(
    backend: str, operation: str, tmp_path: Path
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        db = storage.database
        owner, conversation, job, _ = await source(db)
        engine = JobEngine(db)
        async with db.sessions() as sql:
            step = await sql.scalar(
                select(JobStepRecord.id).where(
                    JobStepRecord.job_id == job, JobStepRecord.status == "running"
                )
            )
        assert step is not None

        async def erase(sql: AsyncSession) -> None:
            assert await purge_conversation(sql, conversation, user_id=owner)

        async def follow() -> None:
            if operation == "start":
                with pytest.raises(RuntimeError, match="job_claim_lost"):
                    await engine.start_step(
                        job, "late secret", checkpoint={"private": "late secret"}
                    )
            elif operation == "complete":
                assert not await engine.complete_step(step, progress=1)
            else:
                await engine.fail_step(
                    step, error_code="late secret", error_detail={"private": "late secret"}
                )

        await during_commit(db, erase, follow)
        async with db.sessions() as sql:
            saved = await sql.get_one(JobRecord, job)
            assert saved.error_code is None and saved.error_detail_safe is None
            assert saved.input == {"source_deleted": True}
            assert all(
                row.checkpoint is None and "secret" not in row.name
                for row in await sql.scalars(
                    select(JobStepRecord).where(JobStepRecord.job_id == job)
                )
            )
