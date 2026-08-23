from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import create_device_routers
from app.auth import AuthService
from app.db import (
    Base,
    Database,
    DeviceClientRecord,
    DevicePairingCodeRecord,
    create_database,
)
from app.devices import DeviceRegistry, PairingCodeInvalid


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


async def _app_with_owner(database: Database) -> tuple[FastAPI, DeviceRegistry]:
    await AuthService(database).setup(
        display_name="Owner",
        password="correct horse battery staple",
    )
    registry = DeviceRegistry(database)
    app = FastAPI()
    admin, devices = create_device_routers(registry, admin_token="admin-token")
    app.include_router(admin)
    app.include_router(devices)
    return app, registry


async def test_pair_heartbeat_admin_update_and_revoke(database: Database) -> None:
    app, registry = await _app_with_owner(database)
    admin_headers = {"Authorization": "Bearer admin-token"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthorized = await client.post(
            "/api/v1/admin/devices/pairing-codes",
            json={"granted_capabilities": ["screen.capture"]},
        )
        pairing = await client.post(
            "/api/v1/admin/devices/pairing-codes",
            headers=admin_headers,
            json={
                "granted_capabilities": ["screen.capture"],
                "ttl_seconds": 600,
            },
        )
        pairing_code = pairing.json()["pairing_code"]
        paired = await client.post(
            "/api/v1/devices/pair",
            json={
                "pairing_code": pairing_code,
                "name": "MacBook Pro",
                "alias": "我的电脑",
                "client_type": "desktop",
                "capabilities": ["screen.capture"],
            },
        )
        reused = await client.post(
            "/api/v1/devices/pair",
            json={
                "pairing_code": pairing_code,
                "name": "Second client",
                "client_type": "desktop",
            },
        )
        token = paired.json()["access_token"]
        device_headers = {"Authorization": f"Bearer {token}"}
        heartbeat = await client.post(
            "/api/v1/devices/heartbeat",
            headers=device_headers,
            json={"capabilities": ["screen.capture", "browser.inspect"]},
        )
        listed = await client.get("/api/v1/admin/devices", headers=admin_headers)
        device_id = paired.json()["device"]["id"]
        stale = await client.patch(
            f"/api/v1/admin/devices/{device_id}",
            headers=admin_headers,
            json={"expected_revision": 1, "name": "stale update"},
        )
        updated = await client.patch(
            f"/api/v1/admin/devices/{device_id}",
            headers=admin_headers,
            json={
                "expected_revision": heartbeat.json()["revision"],
                "name": "工作电脑",
                "granted_capabilities": ["screen.capture", "browser.inspect"],
            },
        )

        actions = await registry.available_actions(
            UUID(paired.json()["device"]["owner_user_id"])
        )
        revoked = await client.post(
            f"/api/v1/admin/devices/{device_id}/revoke",
            headers=admin_headers,
        )
        rejected = await client.post(
            "/api/v1/devices/heartbeat",
            headers=device_headers,
            json={"capabilities": ["screen.capture"]},
        )

    assert unauthorized.status_code == 401
    assert pairing.status_code == 201
    assert paired.status_code == 201
    assert paired.json()["device"]["effective_capabilities"] == ["screen.capture"]
    assert reused.status_code == 401
    assert heartbeat.status_code == 200 and heartbeat.json()["revision"] == 2
    assert listed.status_code == 200 and len(listed.json()) == 1
    assert stale.status_code == 409
    assert updated.status_code == 200
    assert updated.json()["effective_capabilities"] == [
        "browser.inspect",
        "screen.capture",
    ]
    assert {item.label for item in actions} == {
        "我的电脑 · browser.inspect",
        "我的电脑 · screen.capture",
    }
    assert revoked.status_code == 200 and revoked.json()["online"] is False
    assert rejected.status_code == 401

    async with database.sessions() as session:
        record = await session.scalar(select(DeviceClientRecord))
    assert record is not None
    assert record.credential_hash != token
    assert len(record.credential_hash) == 64


async def test_expired_pairing_code_is_rejected(database: Database) -> None:
    _, registry = await _app_with_owner(database)
    pairing = await registry.create_pairing_code(
        owner_user_id=None,
        granted_capabilities=("screen.capture",),
    )
    async with database.sessions.begin() as session:
        record = await session.scalar(select(DevicePairingCodeRecord))
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(PairingCodeInvalid, match="expired"):
        await registry.pair(
            pairing_code=pairing.code,
            name="Expired client",
            alias=None,
            client_type="desktop",
            capabilities=("screen.capture",),
        )


async def test_capability_snapshot_never_escalates_grants(database: Database) -> None:
    _, registry = await _app_with_owner(database)
    pairing = await registry.create_pairing_code(
        owner_user_id=None,
        granted_capabilities=("screen.capture",),
    )
    paired = await registry.pair(
        pairing_code=pairing.code,
        name="Untrusted client",
        alias=None,
        client_type="desktop",
        capabilities=("screen.capture", "device.shell"),
    )
    principal = await registry.authenticate(paired.access_token)
    heartbeat = await registry.heartbeat(
        principal,
        capabilities=("screen.capture", "device.shell", "camera.capture"),
    )

    assert heartbeat.capabilities == (
        "camera.capture",
        "device.shell",
        "screen.capture",
    )
    assert heartbeat.effective_capabilities == ("screen.capture",)
    actions = await registry.available_actions(principal.owner_user_id)
    assert [item.label for item in actions] == ["Untrusted client · screen.capture"]
