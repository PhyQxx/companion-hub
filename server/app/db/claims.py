"""Fence derived writes with an owned row lock on PostgreSQL and SQLite."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from app.harness.claim import ClaimInvalidated, ExecutionClaim, current_claims
from app.harness.time import utc

from .models import JobRecord

_FENCE_KEY = "harness.claim_commit_fence"


@dataclass(slots=True)
class _ClaimCommitFence:
    expiry: datetime | None = None
    lock_scopes: set[SessionTransaction] = field(default_factory=set)
    invalidated: bool = False

    def check(self, session: Session, *context: Any) -> None:
        if self.invalidated or (self.expiry is not None and self.expiry <= datetime.now(UTC)):
            raise ClaimInvalidated()

    def ended(self, session: Session, transaction: SessionTransaction) -> None:
        if transaction.parent is None:
            self.expiry = None
            self.lock_scopes.clear()
            self.invalidated = False

    def rolled_back(self, session: Session, transaction: SessionTransaction) -> None:
        # Rolling back a savepoint can release its claim row locks. A cached
        # expiry alone cannot authorize the surviving outer transaction.
        for scope in self.lock_scopes:
            ancestor: SessionTransaction | None = scope
            while ancestor is not None:
                if ancestor is transaction:
                    self.invalidated = True
                    return
                ancestor = ancestor.parent


def _track_commit_expiry(session: AsyncSession, expiry: datetime) -> None:
    sync = session.sync_session
    fence = sync.info.get(_FENCE_KEY)
    if not isinstance(fence, _ClaimCommitFence):
        fence = _ClaimCommitFence()
        sync.info[_FENCE_KEY] = fence
        event.listen(sync, "before_commit", fence.check)
        event.listen(sync, "before_flush", fence.check)
        event.listen(sync, "after_flush_postexec", fence.check)
        event.listen(sync, "after_transaction_end", fence.ended)
        event.listen(sync, "after_soft_rollback", fence.rolled_back)
    if fence.expiry is None or expiry < fence.expiry:
        fence.expiry = expiry
    transaction = sync.get_nested_transaction() or sync.get_transaction()
    assert transaction is not None
    fence.lock_scopes.add(transaction)


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
    earliest_expiry: datetime | None = None
    for claim in current_claims():
        expiry = await _assert_claim(session, claim)
        if earliest_expiry is None or expiry < earliest_expiry:
            earliest_expiry = expiry
    # WHERE parameters are built before an UPDATE waits for a row lock. Check
    # again after every ancestor is locked, including parents that expired
    # while acquiring a later child's lock. These locks remain through commit.
    if earliest_expiry is not None and earliest_expiry <= datetime.now(UTC):
        raise ClaimInvalidated()
    if earliest_expiry is not None:
        _track_commit_expiry(session, earliest_expiry)


async def _assert_claim(session: AsyncSession, claim: ExecutionClaim) -> datetime:
    # SQLite ignores FOR UPDATE, and an ORM identity-map hit can retain old
    # fields. A conditional no-op write locks the fence through derived commit
    # on both databases, without loading a worker's potentially private payload.
    result = await session.execute(
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
        .returning(JobRecord.id, JobRecord.lease_expires_at)
        .execution_options(synchronize_session=False)
    )
    row = result.one_or_none()
    if row is None or row.lease_expires_at is None:
        raise ClaimInvalidated()
    return utc(row.lease_expires_at)
