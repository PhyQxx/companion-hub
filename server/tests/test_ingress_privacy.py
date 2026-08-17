from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db import Base, Database, EventRecord, create_database
from app.ingress import DatabaseInputSink, EphemeralBuffer, GuardedInputSink
from app.observability import redact_fields
from app.privacy import (
    EgressBlocked,
    EgressDestination,
    EgressGuard,
    L3PersistenceBlocked,
    PrivacyPolicyRegistry,
)
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
    now = datetime.now(UTC)
    first = EphemeralSignal.model_validate(
        {
            **ephemeral_signal.model_dump(mode="python"),
            "occurred_at": now,
            "expires_at": now + timedelta(minutes=2),
            "privacy_level": "L0",
        }
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


async def test_sample_backpressure_keeps_existing_signal(
    ephemeral_signal: EphemeralSignal,
) -> None:
    buffer = EphemeralBuffer(maxsize=1, overflow="sample")
    replacement = EphemeralSignal.model_validate(
        {
            **ephemeral_signal.model_dump(mode="python"),
            "signal_id": "0198b2f4-3b00-7ffe-8000-000000000ffe",
        }
    )
    assert await buffer.put(ephemeral_signal, now=ephemeral_signal.occurred_at)
    assert not await buffer.put(replacement, now=replacement.occurred_at)
    assert await buffer.get(now=ephemeral_signal.occurred_at) == ephemeral_signal
    assert buffer.metrics.dropped == 1


async def test_aggregate_backpressure_combines_numeric_telemetry(
    ephemeral_signal: EphemeralSignal,
) -> None:
    buffer = EphemeralBuffer(maxsize=1, overflow="aggregate")
    replacement = EphemeralSignal.model_validate(
        {
            **ephemeral_signal.model_dump(mode="python"),
            "signal_id": "0198b2f4-3b00-7ffd-8000-000000000ffd",
            "content": {"type": "telemetry", "channel": "pressure", "value": 0.27},
        }
    )
    await buffer.put(ephemeral_signal, now=ephemeral_signal.occurred_at)
    await buffer.put(replacement, now=replacement.occurred_at)
    aggregated = await buffer.get(now=replacement.occurred_at)
    assert aggregated.content.type == "telemetry"
    assert aggregated.content.value == pytest.approx(0.5)
    assert buffer.metrics.aggregated == 1


def test_egress_guard_blocks_l3_and_cloud_l2_without_payload_leakage() -> None:
    guard = EgressGuard()
    cloud = EgressDestination(
        name="mock-cloud", runs_local=False, max_privacy_level=PrivacyLevel.L2
    )
    with pytest.raises(EgressBlocked) as l3_error:
        guard.authorize("L3", cloud)
    with pytest.raises(EgressBlocked) as l2_error:
        guard.authorize("L2", cloud)
    assert str(l3_error.value) == "l3_egress_blocked"
    assert str(l2_error.value) == "l2_requires_local_destination"
    guard.authorize("L1", cloud)


def test_structured_log_redaction_removes_nested_l3_canary() -> None:
    canary = "L3-CANARY-never-log-this"
    result = redact_fields(
        {
            "event_id": "safe-id",
            "content": canary,
            "nested": {"prompt": canary},
            "items": [{"text": canary}],
        }
    )
    assert canary not in repr(result)
    assert result["event_id"] == "safe-id"
