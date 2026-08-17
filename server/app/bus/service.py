from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import TypeAlias
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import ConsumerInboxRecord, EventRecord, OutboxRecord
from app.schemas import InputEnvelope

EventHandler: TypeAlias = Callable[[AsyncSession, InputEnvelope], Awaitable[None]]


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def envelope_to_record(event: InputEnvelope) -> EventRecord:
    return EventRecord(
        event_id=event.event_id,
        proto_version=event.proto_version,
        schema_ref=event.schema_ref,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        user_id=event.user_id,
        conversation_id=event.conversation_id,
        turn_id=event.turn_id,
        source=event.source.model_dump(mode="json"),
        type=event.kind,
        payload={
            "content": [part.model_dump(mode="json") for part in event.content],
            "extensions": event.extensions,
        },
        priority=str(event.priority),
        privacy_level=str(event.privacy_level),
        occurred_at=event.occurred_at,
        received_at=event.received_at,
    )


def record_to_envelope(record: EventRecord) -> InputEnvelope:
    return InputEnvelope.model_validate(
        {
            "proto_version": record.proto_version,
            "schema_ref": record.schema_ref,
            "event_id": record.event_id,
            "correlation_id": record.correlation_id,
            "causation_id": record.causation_id,
            "user_id": record.user_id,
            "conversation_id": record.conversation_id,
            "turn_id": record.turn_id,
            "source": record.source,
            "kind": record.type,
            "occurred_at": _as_aware(record.occurred_at),
            "received_at": _as_aware(record.received_at),
            "priority": record.priority,
            "privacy_level": record.privacy_level,
            "content": record.payload["content"],
            "extensions": record.payload.get("extensions", {}),
        }
    )


async def append_event(
    session: AsyncSession,
    event: InputEnvelope,
    *,
    topics: Iterable[str],
) -> bool:
    """Atomically append an event and its outbox records.

    Returns False when event_id already exists. The caller owns the outer transaction.
    """

    try:
        async with session.begin_nested():
            session.add(envelope_to_record(event))
            await session.flush()
            session.add_all(
                OutboxRecord(event_id=event.event_id, topic=topic)
                for topic in dict.fromkeys(topics)
            )
            await session.flush()
    except IntegrityError:
        return False
    return True


async def consume_event(
    session: AsyncSession,
    *,
    consumer_name: str,
    event_id: UUID,
    handler: EventHandler,
) -> bool:
    """Run a consumer and record its inbox claim in the same transaction."""

    try:
        async with session.begin_nested():
            session.add(ConsumerInboxRecord(consumer_name=consumer_name, event_id=event_id))
            await session.flush()
            record = await session.get(EventRecord, event_id)
            if record is None:
                raise LookupError(f"event not found: {event_id}")
            await handler(session, record_to_envelope(record))
    except IntegrityError:
        return False
    return True
