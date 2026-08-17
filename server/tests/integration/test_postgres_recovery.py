from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.bus import dispatch_once
from app.bus.dispatcher import claim_batch
from app.bus.service import append_event
from app.db import Base, OutboxRecord, create_database
from app.ids import uuid7
from app.schemas import InputEnvelope


class CountingPublisher:
    def __init__(self) -> None:
        self.count = 0
        self.event_ids: list[str] = []

    async def publish(self, topic: str, event: InputEnvelope) -> None:
        del topic
        self.count += 1
        self.event_ids.append(str(event.event_id))
        await asyncio.sleep(0.05)


async def test_postgres_workers_compete_once_and_recover_expired_lease(
    input_event: InputEnvelope,
) -> None:
    database_url = os.getenv("ARIA_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    database_name = make_url(database_url).database or ""
    if not database_name.endswith("_test"):
        raise RuntimeError("integration test refuses to modify a database without a _test suffix")
    database = create_database(database_url)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
            await connection.run_sync(Base.metadata.create_all)
            await connection.exec_driver_sql(
                "CREATE TABLE alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
            await connection.exec_driver_sql(
                "INSERT INTO alembic_version (version_num) VALUES ('0001_event_outbox')"
            )

        async with database.sessions.begin() as session:
            assert await append_event(session, input_event, topics=["hub.internal"])
        publisher = CountingPublisher()
        first, second = await asyncio.gather(
            dispatch_once(database.sessions, publisher, owner="worker-a"),
            dispatch_once(database.sessions, publisher, owner="worker-b"),
        )
        assert first.claimed + second.claimed == 1
        assert first.dispatched + second.dispatched == 1
        assert publisher.count == 1

        recovery_event = InputEnvelope.model_validate(
            {
                **input_event.model_dump(mode="python"),
                "event_id": uuid7(),
                "correlation_id": uuid7(),
            }
        )
        async with database.sessions.begin() as session:
            assert await append_event(session, recovery_event, topics=["hub.recovery"])
        claimed_at = datetime.now(UTC)
        async with database.sessions.begin() as session:
            claimed = await claim_batch(
                session,
                owner="worker-killed-before-publish",
                limit=1,
                lease_seconds=1,
                now=claimed_at,
            )
        assert len(claimed) == 1
        async with database.sessions.begin() as session:
            abandoned = await session.get(OutboxRecord, claimed[0].outbox_id)
            assert abandoned is not None
            abandoned.lease_expires_at = claimed_at - timedelta(seconds=1)

        recovered = await dispatch_once(database.sessions, publisher, owner="worker-restarted")
        assert recovered.claimed == recovered.dispatched == 1
        async with database.sessions() as session:
            dispatched = await session.scalar(
                select(func.count())
                .select_from(OutboxRecord)
                .where(OutboxRecord.status == "dispatched")
            )
        assert dispatched == 2
    finally:
        await database.close()
