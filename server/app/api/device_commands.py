from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import Field, JsonValue, ValidationError

from app.devices import (
    MAX_DEVICE_ASSET_BYTES,
    TERMINAL_COMMAND_STATUSES,
    CommandSnapshot,
    DeviceCommandConflict,
    DeviceCommandNotFound,
    DeviceCommandStore,
    DeviceCredentialInvalid,
    DevicePrincipal,
    DeviceRegistry,
    EphemeralDeviceAssetError,
    EphemeralDeviceAssetStore,
)
from app.schemas.common import NamespacedName, StrictModel

from .admin_config import AdminTokenGuard


class DeviceAuthenticateFrame(StrictModel):
    type: Literal["device.authenticate"]
    access_token: Annotated[str, Field(min_length=32, max_length=256)]
    capabilities: list[NamespacedName] = Field(default_factory=list, max_length=64)


class DeviceHeartbeatFrame(StrictModel):
    type: Literal["device.heartbeat"]
    capabilities: list[NamespacedName] = Field(default_factory=list, max_length=64)


class CommandAckFrame(StrictModel):
    type: Literal["command.ack"]
    command_id: UUID


class CommandResultFrame(StrictModel):
    type: Literal["command.result"]
    command_id: UUID
    outcome: Literal["succeeded", "failed"]
    reason_code: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    result_meta: dict[str, JsonValue] | None = None


class IssueCommandRequest(StrictModel):
    command: NamespacedName
    args: dict[str, JsonValue] = Field(default_factory=dict, max_length=64)
    idempotency_key: Annotated[str, Field(min_length=8, max_length=160)]
    ttl_seconds: Annotated[int, Field(ge=1, le=120)] = 30


class CommandResponse(StrictModel):
    id: UUID
    device_id: UUID
    command: str
    args_redacted: dict[str, JsonValue]
    idempotency_key: str
    status: str
    revision: int
    issued_at: datetime
    expires_at: datetime
    sent_at: datetime | None
    acknowledged_at: datetime | None
    completed_at: datetime | None
    reason_code: str | None
    result_meta: dict[str, JsonValue] | None


class DeviceAssetResponse(StrictModel):
    asset_id: UUID
    command_id: UUID
    media_type: str
    bytes: int
    sha256: str
    expires_at: datetime


@dataclass(slots=True)
class DeviceCommandConnection:
    websocket: WebSocket
    principal: DevicePrincipal
    access_token: str
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send_signed(self, payload: dict[str, Any]) -> None:
        frame = dict(payload)
        frame["signature"] = sign_device_frame(self.access_token, frame)
        async with self.send_lock:
            await self.websocket.send_json(frame)


class DeviceCommandGateway:
    def __init__(
        self,
        registry: DeviceRegistry,
        store: DeviceCommandStore,
        *,
        assets: EphemeralDeviceAssetStore | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self.assets = assets or EphemeralDeviceAssetStore()
        self._connections: dict[UUID, DeviceCommandConnection] = {}
        self._timeout_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._completion_events: dict[UUID, asyncio.Event] = {}

    def connection_for(self, device_id: UUID) -> DeviceCommandConnection | None:
        return self._connections.get(device_id)

    async def connect(self, connection: DeviceCommandConnection) -> None:
        previous = self._connections.get(connection.principal.device_id)
        self._connections[connection.principal.device_id] = connection
        if previous is not None and previous is not connection:
            with suppress(WebSocketDisconnect, RuntimeError):
                await previous.websocket.close(code=4410, reason="replaced by newer connection")

    def disconnect(self, connection: DeviceCommandConnection) -> None:
        current = self._connections.get(connection.principal.device_id)
        if current is connection:
            self._connections.pop(connection.principal.device_id, None)

    async def issue(
        self,
        *,
        device_id: UUID,
        command: str,
        args: dict[str, JsonValue],
        idempotency_key: str,
        ttl_seconds: int,
    ) -> CommandSnapshot:
        issued = await self._store.create(
            device_id=device_id,
            command_name=command,
            args=dict(args),
            idempotency_key=idempotency_key,
            ttl_seconds=ttl_seconds,
        )
        if not issued.created:
            return issued.command
        device = await self._registry.get_device(device_id)
        if command not in device.effective_capabilities:
            result = await self._store.mark_delivery_failed(
                issued.command.id, reason_code="capability_not_authorized"
            )
            self._notify_terminal(result.id)
            return result
        connection = self._connections.get(device_id)
        if connection is None:
            result = await self._store.mark_delivery_failed(
                issued.command.id, reason_code="device_offline"
            )
            self._notify_terminal(result.id)
            return result
        payload = {
            "proto_version": 1,
            "type": "command.execute",
            "command_id": str(issued.command.id),
            "command": command,
            # Sign stable serialized args so Python/JavaScript number formatting
            # differences cannot invalidate an otherwise legitimate frame.
            "args_json": json.dumps(
                args,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "idempotency_key": idempotency_key,
            "issued_at": issued.command.issued_at.isoformat(),
            "expires_at": issued.command.expires_at.isoformat(),
        }
        try:
            await connection.send_signed(payload)
            result = await self._store.mark_sent(issued.command.id)
        except Exception:
            self.disconnect(connection)
            result = await self._store.mark_delivery_failed(
                issued.command.id, reason_code="device_disconnected"
            )
            self._notify_terminal(result.id)
            return result
        self._schedule_timeout(result)
        return result

    async def acknowledge(self, device_id: UUID, command_id: UUID) -> CommandSnapshot:
        return await self._store.acknowledge(device_id, command_id)

    async def complete(
        self,
        device_id: UUID,
        command_id: UUID,
        *,
        outcome: Literal["succeeded", "failed"],
        reason_code: str | None,
        result_meta: dict[str, JsonValue] | None,
    ) -> CommandSnapshot:
        if result_meta is not None and len(
            json.dumps(
                result_meta, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ) > 4096:
            raise DeviceCommandConflict("result_meta exceeds 4096 bytes")
        result = await self._store.complete(
            device_id,
            command_id,
            outcome=outcome,
            reason_code=reason_code,
            result_meta=dict(result_meta) if result_meta is not None else None,
        )
        self._cancel_timeout(command_id)
        self._notify_terminal(command_id)
        return result

    async def cancel(self, command_id: UUID) -> CommandSnapshot:
        result = await self._store.cancel(command_id)
        self._cancel_timeout(command_id)
        self._notify_terminal(command_id)
        connection = self._connections.get(result.device_id)
        if (
            connection is not None
            and result.status == "cancelled"
            and result.reason_code == "cancelled_by_admin"
        ):
            with suppress(Exception):
                await connection.send_signed(
                    {
                        "proto_version": 1,
                        "type": "command.cancel",
                        "command_id": str(command_id),
                        "cancelled_at": datetime.now(UTC).isoformat(),
                    }
                )
        return result

    async def wait_for_terminal(
        self,
        command_id: UUID,
        *,
        timeout_seconds: float | None = None,
    ) -> CommandSnapshot:
        current = await self._store.get(command_id)
        if current.status in TERMINAL_COMMAND_STATUSES:
            return current
        event = self._completion_events.setdefault(command_id, asyncio.Event())
        current = await self._store.get(command_id)
        if current.status in TERMINAL_COMMAND_STATUSES:
            event.set()
        timeout = timeout_seconds
        if timeout is None:
            timeout = max(0.1, (current.expires_at - datetime.now(UTC)).total_seconds() + 1)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        finally:
            if self._completion_events.get(command_id) is event:
                self._completion_events.pop(command_id, None)
        return await self._store.get(command_id)

    async def heartbeat(
        self, principal: DevicePrincipal, capabilities: tuple[str, ...]
    ) -> None:
        await self._registry.heartbeat(principal, capabilities=capabilities)

    def _schedule_timeout(self, command: CommandSnapshot) -> None:
        self._cancel_timeout(command.id)
        task = asyncio.create_task(
            self._expire_after(command.id, command.expires_at),
            name=f"device-command-timeout-{command.id}",
        )
        self._timeout_tasks[command.id] = task
        task.add_done_callback(lambda _: self._timeout_tasks.pop(command.id, None))

    async def _expire_after(self, command_id: UUID, expires_at: datetime) -> None:
        delay = max(0.0, (expires_at - datetime.now(UTC)).total_seconds())
        await asyncio.sleep(delay)
        result = await self._store.mark_timeout(command_id)
        if result.status in TERMINAL_COMMAND_STATUSES:
            self._notify_terminal(command_id)

    def _cancel_timeout(self, command_id: UUID) -> None:
        task = self._timeout_tasks.pop(command_id, None)
        if task is not None:
            task.cancel()

    def _notify_terminal(self, command_id: UUID) -> None:
        event = self._completion_events.get(command_id)
        if event is not None:
            event.set()


def create_device_command_routers(
    registry: DeviceRegistry,
    store: DeviceCommandStore,
    *,
    admin_token: str | None,
) -> tuple[APIRouter, APIRouter, DeviceCommandGateway]:
    admin = APIRouter(
        tags=["admin-device-commands"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )
    websocket_router = APIRouter(tags=["device-command-websocket"])
    gateway = DeviceCommandGateway(registry, store)

    @admin.post(
        "/api/v1/admin/devices/{device_id}/commands",
        response_model=CommandResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def issue_command(device_id: UUID, body: IssueCommandRequest) -> CommandResponse:
        try:
            result = await gateway.issue(
                device_id=device_id,
                command=body.command,
                args=body.args,
                idempotency_key=body.idempotency_key,
                ttl_seconds=body.ttl_seconds,
            )
        except DeviceCommandNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except DeviceCommandConflict as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        return _command_response(result)

    @admin.get("/api/v1/admin/device-commands", response_model=list[CommandResponse])
    async def list_commands(
        device_id: UUID | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
    ) -> list[CommandResponse]:
        return [
            _command_response(value)
            for value in await store.list(device_id=device_id, limit=limit)
        ]

    @admin.get(
        "/api/v1/admin/device-commands/{command_id}", response_model=CommandResponse
    )
    async def command_detail(command_id: UUID) -> CommandResponse:
        try:
            return _command_response(await store.get(command_id))
        except DeviceCommandNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @admin.post(
        "/api/v1/admin/device-commands/{command_id}/cancel",
        response_model=CommandResponse,
    )
    async def cancel_command(command_id: UUID) -> CommandResponse:
        try:
            return _command_response(await gateway.cancel(command_id))
        except DeviceCommandNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @websocket_router.websocket("/ws/devices")
    async def device_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            async with asyncio.timeout(5):
                raw_auth = await websocket.receive_json()
            auth = DeviceAuthenticateFrame.model_validate(raw_auth)
            principal = await registry.authenticate(auth.access_token)
            await registry.heartbeat(
                principal, capabilities=tuple(auth.capabilities)
            )
        except WebSocketDisconnect:
            return
        except (TimeoutError, ValidationError, DeviceCredentialInvalid):
            with suppress(WebSocketDisconnect, RuntimeError):
                await websocket.close(code=4401, reason="device authentication required")
            return
        connection = DeviceCommandConnection(
            websocket=websocket,
            principal=principal,
            access_token=auth.access_token,
        )
        await gateway.connect(connection)
        await connection.send_signed(
            {
                "proto_version": 1,
                "type": "device.accepted",
                "device_id": str(principal.device_id),
                "heartbeat_interval_seconds": 30,
                "sent_at": datetime.now(UTC).isoformat(),
            }
        )
        try:
            while True:
                raw = await websocket.receive_json()
                try:
                    connection.principal = await registry.authenticate(auth.access_token)
                except DeviceCredentialInvalid:
                    await websocket.close(code=4401, reason="device credential revoked")
                    break
                await _handle_device_frame(gateway, connection, raw)
        except WebSocketDisconnect:
            pass
        finally:
            gateway.disconnect(connection)

    @websocket_router.post(
        "/api/v1/devices/commands/{command_id}/asset",
        response_model=DeviceAssetResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_command_asset(
        command_id: UUID,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> DeviceAssetResponse:
        access_token = _bearer_token(authorization)
        try:
            principal = await registry.authenticate(access_token)
            command = await store.get(command_id)
        except (DeviceCredentialInvalid, DeviceCommandNotFound) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="command not found") from error
        if command.device_id != principal.device_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="command not found")
        accepted_media = {
            "screen.capture": {"image/png", "image/jpeg"},
            "screen.monitor": {"image/png", "image/jpeg"},
            "browser.current_tab.capture": {"image/png", "image/jpeg"},
            "browser.current_tab.read": {"application/json"},
        }
        allowed_media = accepted_media.get(command.command_name)
        if allowed_media is None or command.status not in {
            "sent",
            "acknowledged",
        }:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="command does not accept an asset",
            )
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if media_type not in allowed_media:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="media type is not valid for command",
            )
        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                length = int(declared_length)
            except ValueError as error:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, detail="invalid content length"
                ) from error
            if length < 0:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, detail="invalid content length"
                )
            if length > MAX_DEVICE_ASSET_BYTES:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="asset too large")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_DEVICE_ASSET_BYTES:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="asset too large")
        try:
            asset = await gateway.assets.put(
                owner_user_id=principal.owner_user_id,
                device_id=principal.device_id,
                command_id=command_id,
                media_type=media_type,
                data=bytes(body),
            )
        except EphemeralDeviceAssetError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
        return DeviceAssetResponse(
            asset_id=asset.id,
            command_id=asset.command_id,
            media_type=asset.media_type,
            bytes=len(asset.data),
            sha256=asset.sha256,
            expires_at=asset.expires_at,
        )

    return admin, websocket_router, gateway


async def _handle_device_frame(
    gateway: DeviceCommandGateway,
    connection: DeviceCommandConnection,
    raw: Any,
) -> None:
    try:
        frame_type = raw.get("type") if isinstance(raw, dict) else None
        if frame_type == "device.heartbeat":
            heartbeat_frame = DeviceHeartbeatFrame.model_validate(raw)
            await gateway.heartbeat(
                connection.principal, tuple(heartbeat_frame.capabilities)
            )
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "heartbeat.accepted",
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
        elif frame_type == "command.ack":
            ack_frame = CommandAckFrame.model_validate(raw)
            result = await gateway.acknowledge(
                connection.principal.device_id, ack_frame.command_id
            )
            await connection.send_signed(_receipt_frame(result))
        elif frame_type == "command.result":
            result_frame = CommandResultFrame.model_validate(raw)
            result = await gateway.complete(
                connection.principal.device_id,
                result_frame.command_id,
                outcome=result_frame.outcome,
                reason_code=result_frame.reason_code,
                result_meta=result_frame.result_meta,
            )
            await connection.send_signed(_receipt_frame(result))
        elif frame_type == "ping":
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "pong",
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
        else:
            raise ValueError("unknown frame type")
    except (ValidationError, ValueError, DeviceCommandNotFound, DeviceCommandConflict) as error:
        await connection.send_signed(
            {
                "proto_version": 1,
                "type": "protocol.error",
                "reason_code": type(error).__name__,
                "sent_at": datetime.now(UTC).isoformat(),
            }
        )


def sign_device_frame(access_token: str, payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hmac.new(access_token.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _bearer_token(authorization: str | None) -> str:
    scheme, separator, token = (authorization or "").partition(" ")
    if separator != " " or scheme.casefold() != "bearer" or not token.strip():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="device credential required")
    return token.strip()


def verify_device_signature(access_token: str, payload: dict[str, Any]) -> bool:
    signature = payload.get("signature")
    return isinstance(signature, str) and hmac.compare_digest(
        signature, sign_device_frame(access_token, payload)
    )


def _receipt_frame(command: CommandSnapshot) -> dict[str, Any]:
    return {
        "proto_version": 1,
        "type": "receipt.accepted",
        "command_id": str(command.id),
        "status": command.status,
        "revision": command.revision,
        "sent_at": datetime.now(UTC).isoformat(),
    }


def _command_response(value: CommandSnapshot) -> CommandResponse:
    return CommandResponse(
        id=value.id,
        device_id=value.device_id,
        command=value.command_name,
        args_redacted=value.args_redacted,
        idempotency_key=value.idempotency_key,
        status=value.status,
        revision=value.revision,
        issued_at=value.issued_at,
        expires_at=value.expires_at,
        sent_at=value.sent_at,
        acknowledged_at=value.acknowledged_at,
        completed_at=value.completed_at,
        reason_code=value.reason_code,
        result_meta=value.result_meta,
    )
