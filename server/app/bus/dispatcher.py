from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bus.service import record_to_envelope
from app.db import DeadLetterRecord, EventRecord, OutboxRecord
from app.schemas import InputEnvelope


class EventPublisher(Protocol):
    async def publish(self, topic: str, event: InputEnvelope) -> None: ...


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    outbox_id: int
    lease_owner: str
    topic: str
    attempt: int
    event: InputEnvelope


@dataclass(frozen=True, slots=True)
class DispatchResult:
    claimed: int = 0
    dispatched: int = 0
    retried: int = 0
    dead: int = 0


async def claim_batch(
    session: AsyncSession,
    *,
    owner: str,
    limit: int = 50,
    lease_seconds: int = 30,
    now: datetime | None = None,
) -> list[ClaimedDelivery]:
    current = now or datetime.now(UTC)
    statement = (
        select(OutboxRecord, EventRecord)
        .join(EventRecord, EventRecord.event_id == OutboxRecord.event_id)
        .where(
            or_(
                and_(OutboxRecord.status == "pending", OutboxRecord.available_at <= current),
                and_(
                    OutboxRecord.status == "sending",
                    OutboxRecord.lease_expires_at.is_not(None),
                    OutboxRecord.lease_expires_at <= current,
                ),
            )
        )
        .order_by(OutboxRecord.id)
        .limit(limit)
        .with_for_update(skip_locked=True, of=OutboxRecord)
    )
    rows = (await session.execute(statement)).all()
    claimed: list[ClaimedDelivery] = []
    for outbox, event_record in rows:
        outbox.status = "sending"
        outbox.attempts += 1
        outbox.lease_owner = owner
        outbox.lease_expires_at = current + timedelta(seconds=lease_seconds)
        claimed.append(
            ClaimedDelivery(
                outbox_id=outbox.id,
                lease_owner=owner,
                topic=outbox.topic,
                attempt=outbox.attempts,
                event=record_to_envelope(event_record),
            )
        )
    await session.flush()
    return claimed


async def _mark_success(session: AsyncSession, delivery: ClaimedDelivery, *, now: datetime) -> bool:
    outbox = await session.get(OutboxRecord, delivery.outbox_id)
    if outbox is None or outbox.lease_owner != delivery.lease_owner:
        return False
    outbox.status = "dispatched"
    outbox.dispatched_at = now
    outbox.lease_owner = None
    outbox.lease_expires_at = None
    outbox.last_error = None
    return True


async def _mark_failure(
    session: AsyncSession,
    delivery: ClaimedDelivery,
    *,
    error: Exception,
    max_attempts: int,
    now: datetime,
) -> str:
    outbox = await session.get(OutboxRecord, delivery.outbox_id)
    if outbox is None or outbox.lease_owner != delivery.lease_owner:
        return "lost_lease"
    # Exception messages may contain adapter payloads or secrets. Persist only the type.
    detail = type(error).__name__[:160]
    outbox.last_error = detail
    outbox.lease_owner = None
    outbox.lease_expires_at = None
    if outbox.attempts >= max_attempts:
        outbox.status = "dead"
        session.add(
            DeadLetterRecord(
                outbox_id=outbox.id,
                event_id=outbox.event_id,
                topic=outbox.topic,
                attempts=outbox.attempts,
                error_code="publish_failed",
                error_detail=detail,
            )
        )
        return "dead"
    outbox.status = "pending"
    delay_seconds = min(2 ** max(outbox.attempts - 1, 0), 300)
    outbox.available_at = now + timedelta(seconds=delay_seconds)
    return "retry"


async def dispatch_once(
    sessions: async_sessionmaker[AsyncSession],
    publisher: EventPublisher,
    *,
    owner: str,
    batch_size: int = 50,
    max_attempts: int = 8,
) -> DispatchResult:
    async with sessions.begin() as session:
        claimed = await claim_batch(session, owner=owner, limit=batch_size)

    dispatched = retried = dead = 0
    for delivery in claimed:
        try:
            await publisher.publish(delivery.topic, delivery.event)
        except Exception as error:  # publisher failures are isolated per delivery
            async with sessions.begin() as session:
                result = await _mark_failure(
                    session,
                    delivery,
                    error=error,
                    max_attempts=max_attempts,
                    now=datetime.now(UTC),
                )
            retried += result == "retry"
            dead += result == "dead"
        else:
            async with sessions.begin() as session:
                marked = await _mark_success(session, delivery, now=datetime.now(UTC))
            dispatched += marked

    return DispatchResult(
        claimed=len(claimed),
        dispatched=dispatched,
        retried=retried,
        dead=dead,
    )
