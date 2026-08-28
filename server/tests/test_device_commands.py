from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import JsonValue
from starlette.websockets import WebSocketDisconnect

from app.api import (
    DeviceCommandGateway,
    create_device_command_routers,
    verify_device_signature,
)
from app.api.device_commands import DeviceCommandConnection, PetMessageFrame
from app.auth import AuthService
from app.db import Base, DeviceCommandRecord, create_database
from app.devices import DeviceCommandStore, DevicePrincipal, DeviceRegistry
from app.ids import uuid7
from app.schemas import PrivacyLevel


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
            assert json.loads(execute["args_json"])["destination"] == "private path"
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


async def test_avatar_control_uses_authorized_signed_ephemeral_frame(
    tmp_path: Path,
) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.frames: list[dict[str, object]] = []

        async def send_json(self, frame: dict[str, object]) -> None:
            self.frames.append(frame)

        async def close(self, **_: object) -> None:
            return None

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'avatar-control.db'}")
    gateway = DeviceCommandGateway(DeviceRegistry(database), DeviceCommandStore(database))
    owner_id = uuid7()
    token = "aria-device-avatar-control-secret"
    websocket = FakeWebSocket()
    connection = DeviceCommandConnection(
        websocket=cast(Any, websocket),
        principal=DevicePrincipal(device_id=uuid7(), owner_user_id=owner_id),
        access_token=token,
        capabilities=("avatar.render",),
    )
    await gateway.connect(connection)

    delivered = await gateway.publish_avatar_control(
        owner_id, {"emotion": "happy", "speaking": True, "lipSyncMilli": 700}
    )

    assert delivered == 1
    assert len(websocket.frames) == 1
    frame = websocket.frames[0]
    assert frame["type"] == "avatar.control"
    assert frame["sequence"] == 1
    assert frame["control"] == {
        "emotion": "happy",
        "speaking": True,
        "lipSyncMilli": 700,
    }
    assert verify_device_signature(token, frame)
    connection.capabilities = ()
    assert await gateway.publish_avatar_control(owner_id, {"emotion": "sad"}) == 0
    await database.close()


async def test_pet_message_uses_authorized_signed_lifecycle_frames(
    tmp_path: Path,
) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.frames: list[dict[str, object]] = []

        async def send_json(self, frame: dict[str, object]) -> None:
            self.frames.append(frame)

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'pet-message.db'}")
    gateway = DeviceCommandGateway(DeviceRegistry(database), DeviceCommandStore(database))
    owner_id = uuid7()
    request_id = uuid7()
    token = "aria-device-pet-message-secret"
    websocket = FakeWebSocket()
    connection = DeviceCommandConnection(
        websocket=cast(Any, websocket),
        principal=DevicePrincipal(device_id=uuid7(), owner_user_id=owner_id),
        access_token=token,
        capabilities=("avatar.chat",),
    )
    handled = asyncio.Event()

    async def handle_message(
        user_id: UUID, text: str, privacy_level: PrivacyLevel
    ) -> dict[str, JsonValue]:
        assert user_id == owner_id
        assert text == "今天继续做什么?"
        assert privacy_level is PrivacyLevel.L2
        handled.set()
        return {"conversation_id": str(uuid7()), "message_id": str(uuid7())}

    gateway.set_pet_message_handler(handle_message)
    await gateway.start_pet_message(
        connection,
        PetMessageFrame(
            type="pet.message.send",
            request_id=request_id,
            text=" 今天继续做什么? ",
            privacy_level="L2",
        ),
    )
    await asyncio.wait_for(handled.wait(), timeout=1)
    for _ in range(10):
        if len(websocket.frames) == 2:
            break
        await asyncio.sleep(0)

    assert [frame["type"] for frame in websocket.frames] == [
        "pet.message.accepted",
        "pet.message.completed",
    ]
    assert all(verify_device_signature(token, frame) for frame in websocket.frames)
    assert websocket.frames[1]["request_id"] == str(request_id)
    assert "message_id" in cast(dict[str, object], websocket.frames[1]["result"])
    gateway.disconnect(connection)
    await database.close()


async def test_pet_message_rejects_missing_capability(tmp_path: Path) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.frames: list[dict[str, object]] = []

        async def send_json(self, frame: dict[str, object]) -> None:
            self.frames.append(frame)

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'pet-denied.db'}")
    gateway = DeviceCommandGateway(DeviceRegistry(database), DeviceCommandStore(database))
    websocket = FakeWebSocket()
    token = "aria-device-pet-message-denied"
    connection = DeviceCommandConnection(
        websocket=cast(Any, websocket),
        principal=DevicePrincipal(device_id=uuid7(), owner_user_id=uuid7()),
        access_token=token,
        capabilities=("avatar.render",),
    )

    await gateway.start_pet_message(
        connection,
        PetMessageFrame(
            type="pet.message.send",
            request_id=uuid7(),
            text="你好",
            privacy_level="L1",
        ),
    )

    assert len(websocket.frames) == 1
    assert websocket.frames[0]["type"] == "pet.message.failed"
    assert websocket.frames[0]["reason_code"] == "capability_not_authorized"
    assert verify_device_signature(token, websocket.frames[0])
    await database.close()


async def test_replaced_device_connection_does_not_cancel_new_pet_turn(
    tmp_path: Path,
) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.frames: list[dict[str, object]] = []

        async def send_json(self, frame: dict[str, object]) -> None:
            self.frames.append(frame)

        async def close(self, **_: object) -> None:
            return None

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'pet-reconnect.db'}")
    gateway = DeviceCommandGateway(DeviceRegistry(database), DeviceCommandStore(database))
    owner_id = uuid7()
    device_id = uuid7()
    old_started = asyncio.Event()
    new_started = asyncio.Event()
    new_release = asyncio.Event()

    async def handle_message(
        user_id: UUID, text: str, privacy_level: PrivacyLevel
    ) -> dict[str, JsonValue]:
        del user_id, privacy_level
        if text == "old":
            old_started.set()
            await asyncio.Event().wait()
        new_started.set()
        await new_release.wait()
        return {"conversation_id": str(uuid7()), "message_id": str(uuid7())}

    gateway.set_pet_message_handler(handle_message)
    old_socket = FakeWebSocket()
    new_socket = FakeWebSocket()
    old_connection = DeviceCommandConnection(
        websocket=cast(Any, old_socket),
        principal=DevicePrincipal(device_id=device_id, owner_user_id=owner_id),
        access_token="aria-device-old-connection-token",
        capabilities=("avatar.chat",),
    )
    new_connection = DeviceCommandConnection(
        websocket=cast(Any, new_socket),
        principal=DevicePrincipal(device_id=device_id, owner_user_id=owner_id),
        access_token="aria-device-new-connection-token",
        capabilities=("avatar.chat",),
    )
    await gateway.connect(old_connection)
    await gateway.start_pet_message(
        old_connection,
        PetMessageFrame(
            type="pet.message.send", request_id=uuid7(), text="old", privacy_level="L1"
        ),
    )
    await asyncio.wait_for(old_started.wait(), timeout=1)

    await gateway.connect(new_connection)
    await asyncio.sleep(0)
    await gateway.start_pet_message(
        new_connection,
        PetMessageFrame(
            type="pet.message.send", request_id=uuid7(), text="new", privacy_level="L1"
        ),
    )
    await asyncio.wait_for(new_started.wait(), timeout=1)
    gateway.disconnect(old_connection)
    new_release.set()
    for _ in range(10):
        if any(frame["type"] == "pet.message.completed" for frame in new_socket.frames):
            break
        await asyncio.sleep(0)

    assert [frame["type"] for frame in new_socket.frames] == [
        "pet.message.accepted",
        "pet.message.completed",
    ]
    gateway.disconnect(new_connection)
    await database.close()


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


async def test_gateway_wait_for_terminal_is_woken_by_result(tmp_path: Path) -> None:
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'wait.db'}")
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
        name="Wait client",
        alias=None,
        client_type="desktop",
        capabilities=("screen.capture",),
    )
    store = DeviceCommandStore(database)
    issued = await store.create(
        device_id=paired.device.id,
        command_name="screen.capture",
        args={"target": "main_display"},
        idempotency_key="wait-command-0001",
        ttl_seconds=30,
    )
    await store.mark_sent(issued.command.id)
    gateway = DeviceCommandGateway(registry, store)

    waiter = asyncio.create_task(
        gateway.wait_for_terminal(issued.command.id, timeout_seconds=1)
    )
    await asyncio.sleep(0)
    await gateway.complete(
        paired.device.id,
        issued.command.id,
        outcome="succeeded",
        reason_code=None,
        result_meta={"asset_id": str(uuid7())},
    )

    result = await waiter
    assert result.status == "succeeded"
    await database.close()
