from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.auth import AuthService
from app.db import Base, Database, DeviceClientRecord, create_database
from app.devices import (
    DeviceRegistry,
    DeviceTargetAmbiguous,
    DeviceTargetNotFound,
    DeviceTargetResolver,
    DeviceTargetUnavailable,
    PairedDevice,
)


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


async def _registry(database: Database) -> DeviceRegistry:
    await AuthService(database).setup(
        display_name="Owner",
        password="correct horse battery staple",
    )
    return DeviceRegistry(database)


async def _pair(
    registry: DeviceRegistry,
    *,
    name: str,
    alias: str | None,
    client_type: str = "desktop",
    capabilities: tuple[str, ...] = ("screen.capture",),
) -> PairedDevice:
    pairing = await registry.create_pairing_code(
        owner_user_id=None,
        granted_capabilities=capabilities,
    )
    return await registry.pair(
        pairing_code=pairing.code,
        name=name,
        alias=alias,
        client_type=client_type,
        capabilities=capabilities,
    )


async def test_resolves_exact_alias_and_device_id(database: Database) -> None:
    registry = await _registry(database)
    paired = await _pair(registry, name="MacBook Pro", alias="我的电脑")
    resolver = DeviceTargetResolver(registry)

    by_alias = await resolver.resolve(
        owner_user_id=paired.device.owner_user_id,
        target=" 我的电脑 ",
        capability="screen.capture",
    )
    by_id = await resolver.resolve(
        owner_user_id=paired.device.owner_user_id,
        target=str(paired.device.id),
        capability="screen.capture",
    )

    assert by_alias.id == paired.device.id
    assert by_id.id == paired.device.id


async def test_generic_target_selects_only_available_desktop(database: Database) -> None:
    registry = await _registry(database)
    available = await _pair(registry, name="Work Mac", alias="工作电脑")
    await _pair(
        registry,
        name="Ping only",
        alias="备用电脑",
        capabilities=("device.ping",),
    )

    resolved = await DeviceTargetResolver(registry).resolve(
        owner_user_id=available.device.owner_user_id,
        target="computer",
        capability="screen.capture",
    )

    assert resolved.id == available.device.id


async def test_generic_target_selects_browser_for_browser_capability(
    database: Database,
) -> None:
    registry = await _registry(database)
    await _pair(registry, name="Work Mac", alias="工作电脑")
    browser = await _pair(
        registry,
        name="Chrome",
        alias="我的浏览器",
        client_type="browser",
        capabilities=("browser.current_tab.read",),
    )

    resolved = await DeviceTargetResolver(registry).resolve(
        owner_user_id=browser.device.owner_user_id,
        target="我的电脑",
        capability="browser.current_tab.read",
    )

    assert resolved.id == browser.device.id


async def test_generic_target_requires_confirmation_for_multiple_devices(
    database: Database,
) -> None:
    registry = await _registry(database)
    first = await _pair(registry, name="Work Mac", alias="工作电脑")
    second = await _pair(registry, name="Home Mac", alias="家里电脑")

    with pytest.raises(DeviceTargetAmbiguous) as caught:
        await DeviceTargetResolver(registry).resolve(
            owner_user_id=first.device.owner_user_id,
            target="我的电脑",
            capability="screen.capture",
        )

    assert {item.device_id for item in caught.value.candidates} == {
        first.device.id,
        second.device.id,
    }


async def test_explicit_offline_target_does_not_fall_back(database: Database) -> None:
    registry = await _registry(database)
    offline = await _pair(registry, name="Office Mac", alias="办公室电脑")
    await _pair(registry, name="Home Mac", alias="家里电脑")
    async with database.sessions.begin() as session:
        record = await session.scalar(
            select(DeviceClientRecord).where(DeviceClientRecord.id == offline.device.id)
        )
        assert record is not None
        record.last_seen_at = datetime.now(UTC) - timedelta(minutes=5)

    with pytest.raises(DeviceTargetUnavailable) as caught:
        await DeviceTargetResolver(registry).resolve(
            owner_user_id=offline.device.owner_user_id,
            target="办公室电脑",
            capability="screen.capture",
        )

    assert [item.device_id for item in caught.value.candidates] == [offline.device.id]


async def test_unknown_explicit_target_is_not_silently_selected(database: Database) -> None:
    registry = await _registry(database)
    paired = await _pair(registry, name="MacBook Pro", alias="我的电脑")

    with pytest.raises(DeviceTargetNotFound):
        await DeviceTargetResolver(registry).resolve(
            owner_user_id=paired.device.owner_user_id,
            target="不存在的电脑",
            capability="screen.capture",
        )
