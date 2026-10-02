"""Check the request-local fencing identity in the same transaction as a derived write."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.harness.claim import ClaimInvalidated, current_claim

from .models import JobRecord


async def assert_current_claim(session: AsyncSession) -> None:
    claim = current_claim()
    if claim is None:
        return
    job = await session.get(JobRecord, claim.job_id, with_for_update=True)
    if job is None:
        raise ClaimInvalidated()
    expires = job.lease_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if (
        job.lease_owner != claim.worker_id
        or job.attempts != claim.version
        or job.status not in {"admitted", "running"}
        or job.cancel_requested_at is not None
        or expires is None
        or expires <= datetime.now(UTC)
    ):
        raise ClaimInvalidated()
