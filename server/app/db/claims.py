"""Fence derived writes with an owned row lock on PostgreSQL and SQLite."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.harness.claim import ClaimInvalidated, ExecutionClaim, current_claims

from .models import JobRecord


async def lock_job(session: AsyncSession, job_id: UUID) -> JobRecord | None:
    record = await session.scalar(
        update(JobRecord)
        .where(JobRecord.id == job_id)
        .values(progress=JobRecord.progress)
        .returning(JobRecord)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return record if isinstance(record, JobRecord) else None


async def assert_current_claim(session: AsyncSession) -> None:
    for claim in current_claims():
        await _assert_claim(session, claim)


async def _assert_claim(session: AsyncSession, claim: ExecutionClaim) -> None:
    # SQLite ignores FOR UPDATE, and an ORM identity-map hit can retain old
    # fields. A conditional no-op write locks the fence through derived commit
    # on both databases, without loading a worker's potentially private payload.
    identifier = await session.scalar(
        update(JobRecord)
        .where(
            JobRecord.id == claim.job_id,
            JobRecord.lease_owner == claim.worker_id,
            JobRecord.attempts == claim.version,
            JobRecord.status.in_({"admitted", "running"}),
            JobRecord.cancel_requested_at.is_(None),
            JobRecord.lease_expires_at > datetime.now(UTC),
        )
        .values(progress=JobRecord.progress)
        .returning(JobRecord.id)
        .execution_options(synchronize_session=False)
    )
    if identifier is None:
        raise ClaimInvalidated()
