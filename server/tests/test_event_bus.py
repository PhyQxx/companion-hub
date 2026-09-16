from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bus.dispatcher import claim_batch, dispatch_once
from app.bus.router import LocalEventPublisher
from app.bus.service import append_event, consume_event
from app.bus.worker import DispatcherWorker
from app.db import Base, Database, DeadLetterRecord, EventRecord, OutboxRecord, create_database
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
async def database() -> AsyncIterator[Database]:
    db = create_database("sqlite+aiosqlite:///:memory:")
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.close()


async def test_append_event_and_outbox_are_atomic_and_idempotent(
    database: Database, input_event: InputEnvelope
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


async def test_consumer_inbox_runs_handler_once(
    database: Database, input_event: InputEnvelope
) -> None:
    async with database.sessions.begin() as session:
        await append_event(session, input_event, topics=[])

    handled: list[str] = []

    async def handler(session: AsyncSession, event: InputEnvelope) -> None:
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


async def test_failed_consumer_does_not_claim_inbox(
    database: Database, input_event: InputEnvelope
) -> None:
    async with database.sessions.begin() as session:
        await append_event(session, input_event, topics=[])
    attempts = 0

    async def handler(session: AsyncSession, event: InputEnvelope) -> None:
        nonlocal attempts
        del session, event
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic handler failure")

    with pytest.raises(RuntimeError, match="synthetic handler failure"):
        async with database.sessions.begin() as session:
            await consume_event(
                session,
                consumer_name="test.retrying-consumer",
                event_id=input_event.event_id,
                handler=handler,
            )
    async with database.sessions.begin() as session:
        assert await consume_event(
            session,
            consumer_name="test.retrying-consumer",
            event_id=input_event.event_id,
            handler=handler,
        )
    assert attempts == 2


async def test_dispatcher_marks_success(
    database: Database, input_event: InputEnvelope
) -> None:
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
    database: Database, input_event: InputEnvelope
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


async def test_dead_letter_does_not_persist_exception_payload(
    database: Database, input_event: InputEnvelope
) -> None:
    class CanaryPublisher:
        async def publish(self, topic: str, event: InputEnvelope) -> None:
            del topic, event
            raise RuntimeError("L3-CANARY-must-not-enter-dead-letter")

    async with database.sessions.begin() as session:
        await append_event(session, input_event, topics=["hub.internal"])
    await dispatch_once(
        database.sessions,
        CanaryPublisher(),
        owner="worker-canary",
        max_attempts=1,
    )
    async with database.sessions() as session:
        outbox = (await session.scalars(select(OutboxRecord))).one()
        dead_letter = (await session.scalars(select(DeadLetterRecord))).one()
    assert outbox.last_error == "RuntimeError"
    assert dead_letter.error_detail == "RuntimeError"


async def test_expired_dispatch_lease_can_be_reclaimed(
    database: Database, input_event: InputEnvelope
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


async def test_resident_worker_drains_outbox_and_stops(
    database: Database, input_event: InputEnvelope
) -> None:
    async with database.sessions.begin() as session:
        await append_event(session, input_event, topics=["hub.internal"])
    handled = asyncio.Event()

    async def handler(session: AsyncSession, event: InputEnvelope) -> None:
        del session, event
        handled.set()

    publisher = LocalEventPublisher(database)
    publisher.subscribe("hub.internal", "test.worker-consumer", handler)
    worker = DispatcherWorker(database.sessions, publisher, poll_seconds=0.01)
    await worker.start()
    await asyncio.wait_for(handled.wait(), timeout=1)
    await worker.stop()

    assert not worker.state.running
    assert worker.state.dispatched == 1
    async with database.sessions() as session:
        outbox = (await session.scalars(select(OutboxRecord))).one()
    assert outbox.status == "dispatched"


async def test_app_lifespan_exposes_dispatcher_health(
    database: Database, input_event: InputEnvelope
) -> None:
    async with database.sessions.begin() as session:
        await append_event(session, input_event, topics=["hub.internal"])
    publisher = RecordingPublisher()
    app = create_app(database, run_dispatcher=True, event_publisher=publisher)

    async def wait_until_published() -> None:
        while not publisher.events:
            await asyncio.sleep(0.01)

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(wait_until_published(), timeout=1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/healthz")
        assert response.json()["dispatcher"]["running"] is True

    assert app.state.dispatcher.state.running is False


async def test_dev_event_endpoint_appends_event(
    database: Database, input_event: InputEnvelope
) -> None:
    app = create_app(database, enable_dev_endpoints=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/dev/events",
            json={"event": input_event.model_dump(mode="json"), "topics": ["hub.internal"]},
        )
    assert response.status_code == 202
    assert response.json() == {"accepted": True, "event_id": str(input_event.event_id)}


async def test_dev_event_endpoint_requires_admin_token_when_configured(
    database: Database, input_event: InputEnvelope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置了管理令牌的生产部署：dev 事件注入必须携带 Bearer 令牌。"""
    monkeypatch.setenv("ARIA_ADMIN_TOKEN", "test-admin-token")
    app = create_app(database, enable_dev_endpoints=True)
    payload = {"event": input_event.model_dump(mode="json"), "topics": ["hub.internal"]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.post("/api/v1/dev/events", json=payload)
        wrong = await client.post(
            "/api/v1/dev/events",
            json=payload,
            headers={"Authorization": "Bearer wrong-token"},
        )
        authorized = await client.post(
            "/api/v1/dev/events",
            json=payload,
            headers={"Authorization": "Bearer test-admin-token"},
        )
    assert unauthorized.status_code == 401
    assert wrong.status_code == 401
    assert authorized.status_code == 202
