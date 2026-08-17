from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db import Base, Database, EventRecord, create_database
from app.ingress import DatabaseInputSink, EphemeralBuffer, GuardedInputSink
from app.privacy import L3PersistenceBlocked, PrivacyPolicyRegistry
from app.schemas import EphemeralSignal, InputEnvelope, PrivacyLevel


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    db = create_database("sqlite+aiosqlite:///:memory:")
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.close()


async def test_server_policy_blocks_device_that_underreports_l3(
    database: Database, input_event: InputEnvelope
) -> None:
    declared_l0 = InputEnvelope.model_validate(
        {**input_event.model_dump(mode="python"), "privacy_level": "L0"}
    )
    policy = PrivacyPolicyRegistry(
        adapter_levels={input_event.source.adapter_instance_id: PrivacyLevel.L3}
    )
    sink = GuardedInputSink(
        DatabaseInputSink(database, lambda _: ["hub.internal"]),
        EphemeralBuffer(),
        policy,
    )

    with pytest.raises(L3PersistenceBlocked, match="server privacy policy"):
        await sink.emit_durable(declared_l0)

    async with database.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(EventRecord))
    assert count == 0


async def test_server_policy_upgrades_durable_privacy_before_write(
    database: Database, input_event: InputEnvelope
) -> None:
    policy = PrivacyPolicyRegistry(content_levels={"text": PrivacyLevel.L2})
    sink = GuardedInputSink(
        DatabaseInputSink(database, lambda _: []),
        EphemeralBuffer(),
        policy,
    )
    await sink.emit_durable(input_event)
    async with database.sessions() as session:
        record = (await session.scalars(select(EventRecord))).one()
    assert record.privacy_level == "L2"


async def test_ephemeral_policy_upgrade_and_bounded_overflow(
    database: Database,
    ephemeral_signal: EphemeralSignal,
) -> None:
    first = EphemeralSignal.model_validate(
        {**ephemeral_signal.model_dump(mode="python"), "privacy_level": "L0"}
    )
    second = EphemeralSignal.model_validate(
        {
            **first.model_dump(mode="python"),
            "signal_id": "0198b2f4-3b00-7fff-8000-000000000fff",
        }
    )
    buffer = EphemeralBuffer(maxsize=1, overflow="drop_oldest")
    policy = PrivacyPolicyRegistry(channel_levels={"pressure": PrivacyLevel.L3})
    sink = GuardedInputSink(
        DatabaseInputSink(database, lambda _: []),
        buffer,
        policy,
    )
    await sink.emit_ephemeral(first)
    await sink.emit_ephemeral(second)

    received = await buffer.get(now=second.occurred_at)
    assert received.signal_id == second.signal_id
    assert received.privacy_level == "L3"
    assert buffer.metrics.dropped == 1


async def test_ephemeral_buffer_rejects_expired_signal(
    ephemeral_signal: EphemeralSignal,
) -> None:
    buffer = EphemeralBuffer()
    accepted = await buffer.put(
        ephemeral_signal,
        now=ephemeral_signal.expires_at + timedelta(seconds=1),
    )
    assert not accepted
    assert buffer.metrics.expired == 1
    assert buffer.metrics.depth == 0
