from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.bus.dispatcher import claim_batch, dispatch_once
from app.bus.service import append_event, consume_event
from app.db import Base, DeadLetterRecord, EventRecord, OutboxRecord, create_database
from app.main import create_app
from app.schemas import InputEnvelope


class RecordingPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[tuple[str, InputEnvelope]] = []

    async def publish(self, topic: str, event: InputEnvelope) -> None:
        if self.fail:
            raise RuntimeError("synthetic publish failure")
        self.events.append((topic, event))


@pytest.fixture
async def database():
    db = create_database("sqlite+aiosqlite:///:memory:")
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.close()


async def test_append_event_and_outbox_are_atomic_and_idempotent(
    database, input_event: InputEnvelope
) -> None:
    async with database.sessions.begin() as session:
        assert await append_event(session, input_event, topics=["hub.internal", "hub.internal"])
    async with database.sessions.begin() as session:
        assert not await append_event(session, input_event, topics=["hub.internal"])
    async with database.sessions() as session:
        event_count = await session.scalar(select(func.count()).select_from(EventRecord))
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxRecord))
    assert event_count == 1
    assert outbox_count == 1


async def test_consumer_inbox_runs_handler_once(database, input_event: InputEnvelope) -> None:
    async with database.sessions.begin() as session:
        await append_event(session, input_event, topics=[])

    handled: list[str] = []

    async def handler(session, event: InputEnvelope) -> None:
        del session
        handled.append(str(event.event_id))

    async with database.sessions.begin() as session:
        assert await consume_event(
            session,
            consumer_name="test.consumer",
            event_id=input_event.event_id,
            handler=handler,
        )
    async with database.sessions.begin() as session:
        assert not await consume_event(
            session,
            consumer_name="test.consumer",
            event_id=input_event.event_id,
            handler=handler,
        )
    assert handled == [str(input_event.event_id)]


async def test_dispatcher_marks_success(database, input_event: InputEnvelope) -> None:
    async with database.sessions.begin() as session:
        await append_event(session, input_event, topics=["hub.internal"])
    publisher = RecordingPublisher()
    result = await dispatch_once(database.sessions, publisher, owner="worker-1")
    assert result.claimed == result.dispatched == 1
    assert publisher.events == [("hub.internal", input_event)]
    async with database.sessions() as session:
        outbox = (await session.scalars(select(OutboxRecord))).one()
    assert outbox.status == "dispatched"
    assert outbox.lease_owner is None


async def test_dispatcher_moves_terminal_failure_to_dead_letter(
    database, input_event: InputEnvelope
) -> None:
    async with database.sessions.begin() as session:
        await append_event(session, input_event, topics=["hub.internal"])
    result = await dispatch_once(
        database.sessions,
        RecordingPublisher(fail=True),
        owner="worker-1",
        max_attempts=1,
    )
    assert result.dead == 1
    async with database.sessions() as session:
        outbox = (await session.scalars(select(OutboxRecord))).one()
        dead_letter = (await session.scalars(select(DeadLetterRecord))).one()
    assert outbox.status == "dead"
    assert dead_letter.error_code == "publish_failed"


async def test_expired_dispatch_lease_can_be_reclaimed(
    database, input_event: InputEnvelope
) -> None:
    async with database.sessions.begin() as session:
        await append_event(session, input_event, topics=["hub.internal"])
    started = datetime.now(UTC) + timedelta(seconds=1)
    async with database.sessions.begin() as session:
        first = await claim_batch(session, owner="worker-1", lease_seconds=5, now=started)
    assert len(first) == 1
    async with database.sessions.begin() as session:
        second = await claim_batch(
            session,
            owner="worker-2",
            lease_seconds=5,
            now=started + timedelta(seconds=6),
        )
    assert len(second) == 1
    assert second[0].attempt == 2


async def test_dev_event_endpoint_appends_event(database, input_event: InputEnvelope) -> None:
    app = create_app(database, enable_dev_endpoints=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/dev/events",
            json={"event": input_event.model_dump(mode="json"), "topics": ["hub.internal"]},
        )
    assert response.status_code == 202
    assert response.json() == {"accepted": True, "event_id": str(input_event.event_id)}
