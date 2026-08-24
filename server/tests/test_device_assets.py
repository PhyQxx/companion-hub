from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import create_device_command_routers
from app.auth import AuthService
from app.db import Base, create_database
from app.devices import (
    DeviceCommandStore,
    DeviceRegistry,
    EphemeralDeviceAssetError,
    EphemeralDeviceAssetNotFound,
    EphemeralDeviceAssetStore,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


def test_device_upload_is_bound_to_command_and_consumed_once(tmp_path: Path) -> None:
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'assets.db'}")
    registry = DeviceRegistry(database)
    store = DeviceCommandStore(database)

    async def setup() -> tuple[str, UUID, UUID, UUID]:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await AuthService(database).setup(
            display_name="Owner",
            password="correct horse battery staple",
        )
        pairing = await registry.create_pairing_code(
            owner_user_id=None,
            granted_capabilities=("screen.capture", "browser.current_tab.read"),
        )
        paired = await registry.pair(
            pairing_code=pairing.code,
            name="MacBook Pro",
            alias="我的电脑",
            client_type="desktop",
            capabilities=("screen.capture", "browser.current_tab.read"),
        )
        issued = await store.create(
            device_id=paired.device.id,
            command_name="screen.capture",
            args={"target": "main_display"},
            idempotency_key="asset-upload-test-0001",
            ttl_seconds=30,
        )
        await store.mark_sent(issued.command.id)
        browser = await store.create(
            device_id=paired.device.id,
            command_name="browser.current_tab.read",
            args={},
            idempotency_key="browser-asset-upload-test-0001",
            ttl_seconds=30,
        )
        await store.mark_sent(browser.command.id)
        return (
            paired.access_token,
            paired.device.owner_user_id,
            issued.command.id,
            browser.command.id,
        )

    access_token, owner_user_id, command_id, browser_command_id = asyncio.run(setup())
    app = FastAPI()
    admin, device, gateway = create_device_command_routers(
        registry,
        store,
        admin_token="admin-token",
    )
    app.include_router(admin)
    app.include_router(device)

    with TestClient(app) as client:
        unauthenticated = client.post(
            f"/api/v1/devices/commands/{command_id}/asset",
            headers={"Content-Type": "image/png"},
            content=PNG,
        )
        invalid_media = client.post(
            f"/api/v1/devices/commands/{command_id}/asset",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "image/png",
            },
            content=b"not-a-png",
        )
        wrong_command_media = client.post(
            f"/api/v1/devices/commands/{command_id}/asset",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            content=b'{"title":"not allowed for screen capture"}',
        )
        response = client.post(
            f"/api/v1/devices/commands/{command_id}/asset",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "image/png",
            },
            content=PNG,
        )
        browser_response = client.post(
            f"/api/v1/devices/commands/{browser_command_id}/asset",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            content=b'{"title":"Docs","origin":"https://example.com","text":"hello"}',
        )

    assert unauthenticated.status_code == 401
    assert invalid_media.status_code == 422
    assert wrong_command_media.status_code == 422
    assert response.status_code == 201
    assert browser_response.status_code == 201
    payload = response.json()
    asset = asyncio.run(
        gateway.assets.consume(
            UUID(payload["asset_id"]),
            owner_user_id=owner_user_id,
            command_id=command_id,
        )
    )
    assert asset.data == PNG
    assert payload["sha256"] == asset.sha256
    with pytest.raises(EphemeralDeviceAssetNotFound):
        asyncio.run(
            gateway.assets.consume(
                asset.id,
                owner_user_id=owner_user_id,
                command_id=command_id,
            )
        )
    browser_payload = browser_response.json()
    browser_asset = asyncio.run(
        gateway.assets.consume(
            UUID(browser_payload["asset_id"]),
            owner_user_id=owner_user_id,
            command_id=browser_command_id,
        )
    )
    assert browser_asset.media_type == "application/json"
    asyncio.run(database.close())


async def test_ephemeral_asset_validates_media_and_expiry() -> None:
    assets = EphemeralDeviceAssetStore()
    owner_id = UUID("018f5f61-2a65-7a21-a835-1a2b3c4d5e6f")
    device_id = UUID("018f5f61-2a65-7a21-a835-1a2b3c4d5e70")
    command_id = UUID("018f5f61-2a65-7a21-a835-1a2b3c4d5e71")
    now = datetime.now(UTC)
    with pytest.raises(EphemeralDeviceAssetError, match="invalid"):
        await assets.put(
            owner_user_id=owner_id,
            device_id=device_id,
            command_id=command_id,
            media_type="image/png",
            data=b"not-a-png",
        )
    asset = await assets.put(
        owner_user_id=owner_id,
        device_id=device_id,
        command_id=command_id,
        media_type="image/png",
        data=PNG,
        now=now,
    )
    with pytest.raises(EphemeralDeviceAssetNotFound):
        await assets.consume(
            asset.id,
            owner_user_id=owner_id,
            command_id=command_id,
            now=now + timedelta(minutes=3),
        )


async def test_ephemeral_asset_accepts_bounded_browser_document() -> None:
    assets = EphemeralDeviceAssetStore()
    owner_id = UUID("018f5f61-2a65-7a21-a835-1a2b3c4d5e6f")
    device_id = UUID("018f5f61-2a65-7a21-a835-1a2b3c4d5e70")
    command_id = UUID("018f5f61-2a65-7a21-a835-1a2b3c4d5e71")
    document = b'{"title":"Docs","origin":"https://example.com","text":"hello"}'

    asset = await assets.put(
        owner_user_id=owner_id,
        device_id=device_id,
        command_id=command_id,
        media_type="application/json",
        data=document,
    )

    assert asset.data == document
    with pytest.raises(EphemeralDeviceAssetError, match="JSON object"):
        await assets.put(
            owner_user_id=owner_id,
            device_id=device_id,
            command_id=UUID("018f5f61-2a65-7a21-a835-1a2b3c4d5e72"),
            media_type="application/json",
            data=b"[]",
        )
