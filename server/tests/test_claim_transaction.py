"""A source cancellation cannot pass an accepted derived transaction."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import update
from test_cognitive_save_guard import database as save_database
from test_perception import user_id as perception_user

from app.db import Database, JobRecord, SemanticEventAuditRecord
from app.db.claims import assert_current_claim
from app.harness.claim import ClaimInvalidated, ExecutionClaim, claim_scope
from app.ids import uuid7
from app.jobs.engine import JobEngine

database = save_database
user_id = perception_user


async def claim(database: Database, user_id: UUID) -> tuple[JobEngine, ExecutionClaim]:
    engine = JobEngine(database)
    job = await engine.submit("fixture.claim", {}, owner=str(user_id), resource_class="fixture")
    claimed = await engine.claim("fixture-worker", resource_class="fixture", job_id=job.id)
    assert claimed is not None
    return engine, ExecutionClaim(job.id, "fixture-worker", claimed.attempts)


async def test_claim_lock_serializes_cancellation_after_derived_commit(
    database: Database, user_id: UUID
) -> None:
    engine, identity = await claim(database, user_id)
    checked, release, cancel_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    event_id = uuid7()

    async def derived() -> None:
        with claim_scope(identity):
            async with database.sessions.begin() as session:
                await assert_current_claim(session)
                checked.set()
                await release.wait()
                session.add(
                    SemanticEventAuditRecord(
                        event_id=event_id,
                        user_id=user_id,
                        kind="fixture",
                        source_kind="fixture",
                        dedupe_key="fixture",
                        privacy_level="L1",
                        evidence_ids=["fixture"],
                        disposition="processed",
                        occurred_at=datetime.now(UTC),
                        created_at=datetime.now(UTC),
                    )
                )

    async def cancel() -> bool:
        cancel_started.set()
        return await engine.cancel(identity.job_id)

    writer = asyncio.create_task(derived())
    canceller = None
    try:
        await asyncio.wait_for(checked.wait(), 3)
        canceller = asyncio.create_task(cancel())
        await cancel_started.wait()
        await asyncio.sleep(0.1)
        assert not canceller.done(), "cancellation passed the derived transaction's claim lock"
        release.set()
        await asyncio.wait_for(writer, 3)
        assert await asyncio.wait_for(canceller, 3)
    finally:
        release.set()
        if not writer.done():
            writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        if canceller is not None:
            if not canceller.done():
                canceller.cancel()
            await asyncio.gather(canceller, return_exceptions=True)
    async with database.sessions() as session:
        assert await session.get(SemanticEventAuditRecord, event_id) is not None
        job = await session.get_one(JobRecord, identity.job_id)
        assert job.status == "cancelling" and job.progress == 0


@pytest.mark.parametrize("revocation", ["cancel", "version", "lease"])
async def test_cached_job_cannot_hide_a_revoked_claim(
    database: Database, user_id: UUID, revocation: str
) -> None:
    engine, identity = await claim(database, user_id)
    async with database.sessions() as stale:
        cached = await stale.get_one(JobRecord, identity.job_id)
        if revocation == "cancel":
            await engine.cancel(identity.job_id)
        else:
            async with database.sessions.begin() as session:
                await session.execute(
                    update(JobRecord)
                    .where(JobRecord.id == identity.job_id)
                    .values(
                        **(
                            {"attempts": identity.version + 1}
                            if revocation == "version"
                            else {"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)}
                        )
                    )
                )
        assert cached.status == "admitted"
        with claim_scope(identity), pytest.raises(ClaimInvalidated):
            await assert_current_claim(stale)
        await stale.rollback()


async def test_cancel_waiting_for_commit_cannot_overwrite_completed_job(
    database: Database, user_id: UUID
) -> None:
    engine, identity = await claim(database, user_id)
    saved, release = asyncio.Event(), asyncio.Event()

    async def complete() -> None:
        async with database.sessions.begin() as session:
            await session.execute(
                update(JobRecord).where(JobRecord.id == identity.job_id).values(status="succeeded")
            )
            saved.set()
            await release.wait()

    writer = asyncio.create_task(complete())
    canceller = None
    try:
        await asyncio.wait_for(saved.wait(), 3)
        canceller = asyncio.create_task(engine.cancel(identity.job_id))
        await asyncio.sleep(0.1)
        assert not canceller.done()
        release.set()
        await asyncio.wait_for(writer, 3)
        assert not await asyncio.wait_for(canceller, 3)
    finally:
        release.set()
        if not writer.done():
            writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        if canceller is not None:
            if not canceller.done():
                canceller.cancel()
            await asyncio.gather(canceller, return_exceptions=True)
    async with database.sessions() as session:
        row = await session.get_one(JobRecord, identity.job_id)
        assert row.status == "succeeded" and row.cancel_requested_at is None
