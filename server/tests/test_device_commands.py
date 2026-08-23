from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import (
    create_device_command_routers,
    verify_device_signature,
)
from app.auth import AuthService
from app.db import Base, DeviceCommandRecord, create_database
from app.devices import DeviceCommandStore, DeviceRegistry


def test_signed_command_websocket_ack_result_cancel_and_idempotency(
    tmp_path: Path,
) -> None:
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'commands.db'}")
    registry = DeviceRegistry(database)
    store = DeviceCommandStore(database)

    async def setup() -> tuple[str, UUID]:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await AuthService(database).setup(
            display_name="Owner",
            password="correct horse battery staple",
        )
        pairing = await registry.create_pairing_code(
            owner_user_id=None,
            granted_capabilities=("screen.capture", "browser.inspect"),
        )
        paired = await registry.pair(
            pairing_code=pairing.code,
            name="MacBook Pro",
            alias="我的电脑",
            client_type="desktop",
            capabilities=("screen.capture",),
        )
        return paired.access_token, paired.device.id

    access_token, device_id = asyncio.run(setup())
    app = FastAPI()
    admin_router, websocket_router, _ = create_device_command_routers(
        registry,
        store,
        admin_token="admin-token",
    )
    app.include_router(admin_router)
    app.include_router(websocket_router)
    admin_headers = {"Authorization": "Bearer admin-token"}

    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as invalid,
            client.websocket_connect("/ws/devices") as websocket,
        ):
            websocket.send_json(
                {"type": "device.authenticate", "access_token": "invalid-device-token"}
            )
            websocket.receive_json()
        assert invalid.value.code == 4401

        with client.websocket_connect("/ws/devices") as websocket:
            websocket.send_json(
                {
                    "type": "device.authenticate",
                    "access_token": access_token,
                    "capabilities": ["screen.capture", "browser.inspect"],
                }
            )
            accepted = websocket.receive_json()
            assert accepted["type"] == "device.accepted"
            assert verify_device_signature(access_token, accepted)

            issued = client.post(
                f"/api/v1/admin/devices/{device_id}/commands",
                headers=admin_headers,
                json={
                    "command": "screen.capture",
                    "args": {"target": "active_window", "destination": "private path"},
                    "idempotency_key": "capture-turn-0001",
                    "ttl_seconds": 30,
                },
            )
            assert issued.status_code == 202
            assert issued.json()["status"] == "sent"
            assert issued.json()["args_redacted"]["destination"] == "[REDACTED]"
            command_id = issued.json()["id"]

            execute = websocket.receive_json()
            assert execute["type"] == "command.execute"
            assert execute["args"]["destination"] == "private path"
            assert verify_device_signature(access_token, execute)

            websocket.send_json({"type": "command.ack", "command_id": command_id})
            ack = websocket.receive_json()
            assert ack["type"] == "receipt.accepted"
            assert ack["status"] == "acknowledged"
            assert verify_device_signature(access_token, ack)

            websocket.send_json(
                {
                    "type": "command.result",
                    "command_id": command_id,
                    "outcome": "succeeded",
                    "result_meta": {"media_type": "image/jpeg", "bytes": 2048},
                }
            )
            completed = websocket.receive_json()
            assert completed["status"] == "succeeded"
            assert verify_device_signature(access_token, completed)

            duplicate = client.post(
                f"/api/v1/admin/devices/{device_id}/commands",
                headers=admin_headers,
                json={
                    "command": "screen.capture",
                    "args": {"target": "active_window", "destination": "private path"},
                    "idempotency_key": "capture-turn-0001",
                },
            )
            assert duplicate.json()["id"] == command_id
            assert duplicate.json()["status"] == "succeeded"
            conflict = client.post(
                f"/api/v1/admin/devices/{device_id}/commands",
                headers=admin_headers,
                json={
                    "command": "browser.inspect",
                    "args": {},
                    "idempotency_key": "capture-turn-0001",
                },
            )
            assert conflict.status_code == 409

            cancellable = client.post(
                f"/api/v1/admin/devices/{device_id}/commands",
                headers=admin_headers,
                json={
                    "command": "browser.inspect",
                    "args": {},
                    "idempotency_key": "inspect-turn-0002",
                },
            )
            cancel_id = cancellable.json()["id"]
            assert websocket.receive_json()["type"] == "command.execute"
            cancelled = client.post(
                f"/api/v1/admin/device-commands/{cancel_id}/cancel",
                headers=admin_headers,
            )
            assert cancelled.json()["status"] == "cancelled"
            cancel_frame = websocket.receive_json()
            assert cancel_frame["type"] == "command.cancel"
            assert verify_device_signature(access_token, cancel_frame)

        offline = client.post(
            f"/api/v1/admin/devices/{device_id}/commands",
            headers=admin_headers,
            json={
                "command": "screen.capture",
                "args": {},
                "idempotency_key": "offline-turn-0003",
            },
        )
        assert offline.json()["status"] == "failed"
        assert offline.json()["reason_code"] == "device_offline"

        unauthorized_capability = client.post(
            f"/api/v1/admin/devices/{device_id}/commands",
            headers=admin_headers,
            json={
                "command": "device.shell",
                "args": {},
                "idempotency_key": "unauthorized-turn-0004",
            },
        )
        assert unauthorized_capability.json()["status"] == "failed"
        assert (
            unauthorized_capability.json()["reason_code"]
            == "capability_not_authorized"
        )

        listed = client.get(
            "/api/v1/admin/device-commands",
            headers=admin_headers,
            params={"device_id": str(device_id)},
        )
        assert listed.status_code == 200
        assert [item["status"] for item in listed.json()] == [
            "failed",
            "failed",
            "cancelled",
            "succeeded",
        ]

    asyncio.run(database.close())


def test_device_frame_signature_detects_tampering() -> None:
    from app.api import sign_device_frame

    token = "aria_device_test-secret"
    frame = {
        "type": "command.execute",
        "command_id": "123",
        "args": {"target": "active_window"},
    }
    frame["signature"] = sign_device_frame(token, frame)
    assert verify_device_signature(token, frame)
    frame["args"] = {"target": "other_window"}
    assert not verify_device_signature(token, frame)


async def test_sent_command_expires_as_timeout(tmp_path: Path) -> None:
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'timeout.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await AuthService(database).setup(
        display_name="Owner",
        password="correct horse battery staple",
    )
    registry = DeviceRegistry(database)
    pairing = await registry.create_pairing_code(
        owner_user_id=None,
        granted_capabilities=("screen.capture",),
    )
    paired = await registry.pair(
        pairing_code=pairing.code,
        name="Timeout client",
        alias=None,
        client_type="desktop",
        capabilities=("screen.capture",),
    )
    store = DeviceCommandStore(database)
    issued = await store.create(
        device_id=paired.device.id,
        command_name="screen.capture",
        args={},
        idempotency_key="timeout-command-0001",
        ttl_seconds=30,
    )
    sent = await store.mark_sent(issued.command.id)
    async with database.sessions.begin() as session:
        record = await session.get(DeviceCommandRecord, sent.id)
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    timed_out = await store.mark_timeout(sent.id)
    assert timed_out.status == "timed_out"
    assert timed_out.reason_code == "command_timeout"
    assert timed_out.completed_at is not None
    await database.close()
