"""Captured observations retain the original device and capability authority."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select, update
from test_cognitive_save_guard import database as save_database
from test_observation_inflight import fixture_loop

from app.browser_awareness.loop import CAPABILITY as BROWSER_CAPABILITY
from app.context.observation import ObservationUnavailable
from app.db import AppUserRecord, Database, DeviceClientRecord, TimelineEventRecord
from app.devices import DeviceRegistry, DeviceTargetResolver
from app.ids import uuid7
from app.screen_awareness.loop import CAPABILITY as SCREEN_CAPABILITY

database = save_database


def device(owner: UUID, kind: str, device_id: UUID) -> DeviceClientRecord:
    now = datetime.now(UTC)
    capability = BROWSER_CAPABILITY if kind == "browser" else SCREEN_CAPABILITY
    return DeviceClientRecord(
        id=device_id,
        owner_user_id=owner,
        name="Private observation fixture",
        client_type="browser" if kind == "browser" else "desktop",
        credential_hash=device_id.hex * 2,
        capabilities=[capability],
        granted_capabilities=[capability],
        paired_at=now,
        last_seen_at=now,
    )


async def bind_device(
    database: Database, loop: Any, owner: UUID, kind: str, device_id: UUID
) -> None:
    async with database.sessions.begin() as session:
        session.add(device(owner, kind, device_id))
    loop._resolver = DeviceTargetResolver(DeviceRegistry(database))


async def revoke(database: Database, owner: UUID, device_id: UUID, reason: str) -> None:
    now = datetime.now(UTC)
    changes: dict[str, Any] = {
        "revoked": {"revoked_at": now},
        "grants": {"granted_capabilities": []},
        "capabilities": {"capabilities": []},
        "offline": {"last_seen_at": now - timedelta(minutes=5)},
    }
    async with database.sessions.begin() as session:
        if reason == "removed":
            await session.execute(
                delete(DeviceClientRecord).where(DeviceClientRecord.id == device_id)
            )
        else:
            if reason == "owner":
                secondary = await session.scalar(
                    select(AppUserRecord.id).where(AppUserRecord.id != owner)
                )
                assert secondary is not None
                values = {"owner_user_id": secondary}
            else:
                values = changes[reason]
            await session.execute(
                update(DeviceClientRecord)
                .where(DeviceClientRecord.id == device_id)
                .values(**values)
            )


@pytest.mark.parametrize("kind", ["browser", "screen"])
@pytest.mark.parametrize("stage", ["read", "analysis"])
@pytest.mark.parametrize(
    "reason", ["revoked", "grants", "capabilities", "offline", "owner", "removed"]
)
async def test_inflight_device_revocation_cannot_fallback_to_another_available_device(
    database: Database, kind: str, stage: str, reason: str
) -> None:
    loop, config, owner, ports = await fixture_loop(database, kind)
    await bind_device(database, loop, owner, kind, ports.device_id)
    ports.block_stage = stage
    task = asyncio.create_task(loop._tick(config))
    try:
        await asyncio.wait_for(ports.started.wait(), 2)
        await revoke(database, owner, ports.device_id, reason)
        async with database.sessions.begin() as session:
            # A replacement exists, but must not validate the original capture.
            session.add(device(owner, kind, uuid7()))
        outcome = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2))[0]
        assert outcome is None or (
            isinstance(outcome, ObservationUnavailable)
            and outcome.reason_code == "observation_device_changed"
        )
        assert ports.cancelled.is_set()
        assert not ports.memories and not ports.events and not ports.deliveries
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(TimelineEventRecord)) == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["browser", "screen"])
async def test_delayed_event_and_delivery_revalidate_the_captured_device(
    database: Database, kind: str
) -> None:
    loop, config, owner, ports = await fixture_loop(database, kind)
    await bind_device(database, loop, owner, kind, ports.device_id)
    await loop._tick(config)
    assert len(ports.events) == 1
    event, validate, handler = ports.events[0]
    assert event.attributes["device_id"] == str(ports.device_id)
    assert await validate()
    await revoke(database, owner, ports.device_id, "grants")
    assert not await validate()
    await handler(event, SimpleNamespace(decision=SimpleNamespace(decision="inform")))
    assert not ports.deliveries


@pytest.mark.parametrize("kind", ["browser", "screen"])
async def test_missing_device_identity_does_not_fallback_to_owner_only_delivery(
    database: Database, kind: str
) -> None:
    loop, config, owner, ports = await fixture_loop(database, kind)
    await bind_device(database, loop, owner, kind, ports.device_id)
    await loop._tick(config)
    event, _, handler = ports.events[0]
    invalid = event.model_copy(update={"attributes": {"message": "Private"}})
    await handler(invalid, SimpleNamespace(decision=SimpleNamespace(decision="inform")))
    assert not ports.deliveries
