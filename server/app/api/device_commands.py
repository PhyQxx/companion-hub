from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
    DeviceSnapshot,
    EphemeralDeviceAssetError,
    EphemeralDeviceAssetStore,
)
from app.ids import uuid7
from app.satellite import (
    SATELLITE_CAPABILITY,
    InvalidSatelliteTransition,
    SatelliteAudioChunkFrame,
    SatelliteAudioEndFrame,
    SatelliteAudioStartFrame,
    SatelliteCancelFrame,
    SatelliteEvent,
    SatelliteHelloFrame,
    SatelliteRegistry,
    SatelliteState,
    SatelliteStateFrame,
    SatelliteTakeoverFrame,
    SatelliteWakeFrame,
    device_reported_event,
)
from app.schemas import PrivacyLevel
from app.schemas.common import NamespacedName, StrictModel

from .admin_config import AdminTokenGuard


class DeviceAuthenticateFrame(StrictModel):
    type: Literal["device.authenticate"]
    access_token: Annotated[str, Field(min_length=32, max_length=256)]
    capabilities: list[NamespacedName] = Field(default_factory=list, max_length=64)


class DeviceTabHintFrame(StrictModel):
    """心跳轻量指纹（42 号方案 P3）：只有 origin 与标题，永不携带路径、查询串或正文。"""

    origin: Annotated[str, Field(min_length=1, max_length=2_048)]
    title: Annotated[str, Field(max_length=500)] = ""


class DeviceHeartbeatFrame(StrictModel):
    type: Literal["device.heartbeat"]
    capabilities: list[NamespacedName] = Field(default_factory=list, max_length=64)
    tab_hint: DeviceTabHintFrame | None = None


class PetMessageFrame(StrictModel):
    type: Literal["pet.message.send"]
    request_id: UUID
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    privacy_level: Literal["L0", "L1", "L2"] = "L1"
    speak: bool = True


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
    capabilities: tuple[str, ...] = ()
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send_signed(self, payload: dict[str, Any]) -> None:
        frame = dict(payload)
        frame["signature"] = sign_device_frame(self.access_token, frame)
        async with self.send_lock:
            await self.websocket.send_json(frame)


PetMessageHandler = Callable[
    [UUID, str, PrivacyLevel],
    Awaitable[tuple[dict[str, JsonValue], str]],
]
PetAudioEmitter = Callable[[str, dict[str, JsonValue]], Awaitable[None]]
PetAudioHandler = Callable[[str, PrivacyLevel, PetAudioEmitter], Awaitable[bool]]
SatelliteUtteranceHandler = Callable[
    [UUID, UUID, bytes, PrivacyLevel, PetAudioEmitter], Awaitable[bool]
]
SatelliteTakeoverHandler = Callable[[UUID, UUID, UUID], Awaitable[None]]

SATELLITE_AUDIO_MAX_BYTES = 4 * 1024 * 1024
SATELLITE_AUDIO_MIN_BYTES = 4_800


@dataclass(slots=True)
class SatelliteAudioUpload:
    utterance_id: UUID
    privacy_level: PrivacyLevel
    data: bytearray = field(default_factory=bytearray)
    next_index: int = 0


@dataclass(frozen=True, slots=True)
class DeviceTabHint:
    origin: str
    title: str
    updated_at: datetime


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
        self._avatar_sequences: dict[UUID, int] = {}
        self._pet_message_handler: PetMessageHandler | None = None
        self._pet_audio_handler: PetAudioHandler | None = None
        self._pet_message_tasks: dict[tuple[UUID, UUID], asyncio.Task[None]] = {}
        self._pet_message_requests: set[tuple[UUID, UUID]] = set()
        self._satellite_utterance_handler: SatelliteUtteranceHandler | None = None
        self._satellite_takeover_handler: SatelliteTakeoverHandler | None = None
        self._satellite_audio: dict[UUID, SatelliteAudioUpload] = {}
        self._satellite_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._satellite_follow_up_tasks: dict[UUID, asyncio.Task[None]] = {}
        # SAT-01 卫星会话与唤醒仲裁（内存态，掉线即注销）
        self.satellites = SatelliteRegistry()
        # 42 号方案 P3 浏览感知指纹：仅当循环开启时设备才上报，掉线即清空
        self.tab_hint_enabled = False
        self._tab_hints: dict[UUID, DeviceTabHint] = {}

    def set_tab_hint_enabled(self, enabled: bool) -> None:
        self.tab_hint_enabled = enabled

    def update_tab_hint(self, device_id: UUID, *, origin: str, title: str) -> None:
        self._tab_hints[device_id] = DeviceTabHint(
            origin=origin, title=title, updated_at=datetime.now(UTC)
        )

    def tab_hint_for(self, device_id: UUID) -> DeviceTabHint | None:
        return self._tab_hints.get(device_id)

    def set_pet_message_handler(self, handler: PetMessageHandler) -> None:
        self._pet_message_handler = handler

    def set_pet_audio_handler(self, handler: PetAudioHandler) -> None:
        self._pet_audio_handler = handler

    def set_satellite_utterance_handler(self, handler: SatelliteUtteranceHandler) -> None:
        self._satellite_utterance_handler = handler

    def set_satellite_takeover_handler(self, handler: SatelliteTakeoverHandler) -> None:
        self._satellite_takeover_handler = handler

    def connection_for(self, device_id: UUID) -> DeviceCommandConnection | None:
        return self._connections.get(device_id)

    async def connect(self, connection: DeviceCommandConnection) -> None:
        previous = self._connections.get(connection.principal.device_id)
        if previous is not None and previous is not connection:
            self._cancel_pet_messages(connection.principal.device_id)
            self._cancel_satellite(connection.principal.device_id)
        self._connections[connection.principal.device_id] = connection
        if previous is not None and previous is not connection:
            with suppress(WebSocketDisconnect, RuntimeError):
                await previous.websocket.close(code=4410, reason="replaced by newer connection")

    def disconnect(self, connection: DeviceCommandConnection) -> None:
        current = self._connections.get(connection.principal.device_id)
        if current is connection:
            self._connections.pop(connection.principal.device_id, None)
            self._cancel_pet_messages(connection.principal.device_id)
            self._cancel_satellite(connection.principal.device_id)
            self._tab_hints.pop(connection.principal.device_id, None)
            self.satellites.unregister(connection.principal.device_id)

    def _cancel_pet_messages(self, device_id: UUID) -> None:
        for key, task in list(self._pet_message_tasks.items()):
            if key[0] == device_id:
                task.cancel()
                self._pet_message_tasks.pop(key, None)
        self._pet_message_requests = {
            key for key in self._pet_message_requests if key[0] != device_id
        }

    def _cancel_satellite(self, device_id: UUID) -> None:
        self._satellite_audio.pop(device_id, None)
        task = self._satellite_tasks.pop(device_id, None)
        if task is not None:
            task.cancel()
        self._cancel_follow_up_timeout(device_id)
        self.satellites.unregister(device_id)

    def _cancel_follow_up_timeout(self, device_id: UUID) -> None:
        task = self._satellite_follow_up_tasks.pop(device_id, None)
        if task is not None:
            task.cancel()

    async def satellite_hello(
        self,
        connection: DeviceCommandConnection,
        frame: SatelliteHelloFrame,
    ) -> None:
        if SATELLITE_CAPABILITY not in connection.capabilities:
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "satellite.error",
                    "reason_code": "capability_not_authorized",
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
            return
        session = self.satellites.register(
            device_id=connection.principal.device_id,
            owner_user_id=connection.principal.owner_user_id,
            room_id=frame.room_id,
            max_privacy_level=PrivacyLevel(frame.max_privacy_level),
            continuous_timeout_seconds=frame.continuous_timeout_seconds,
        )
        await connection.send_signed(
            {
                "proto_version": 1,
                "type": "satellite.ready",
                "room_id": session.room_id,
                "max_privacy_level": session.max_privacy_level.value,
                "continuous_timeout_seconds": session.continuous_timeout_seconds,
                "state": session.state.value,
                "sent_at": datetime.now(UTC).isoformat(),
            }
        )

    async def satellite_wake(
        self,
        connection: DeviceCommandConnection,
        frame: SatelliteWakeFrame,
    ) -> None:
        if SATELLITE_CAPABILITY not in connection.capabilities:
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "satellite.error",
                    "reason_code": "capability_not_authorized",
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
            return
        session = self.satellites.get(connection.principal.device_id)
        if session is not None and session.room_id != frame.room_id:
            await self._send_satellite_error(connection, "room_mismatch")
            return
        decision = self.satellites.arbitrate_wake(
            device_id=connection.principal.device_id,
            owner_user_id=connection.principal.owner_user_id,
        )
        if decision.winner:
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "satellite.state.set",
                    "state": decision.session_state.value,
                    "room_id": frame.room_id,
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
        else:
            active_device_id = next(
                (
                    session.device_id
                    for session in self.satellites.sessions.values()
                    if session.owner_user_id == connection.principal.owner_user_id
                    and session.state is not SatelliteState.IDLE
                ),
                None,
            )
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "satellite.wake.suppressed",
                    "reason_code": decision.reason_code,
                    "active_device_id": (
                        str(active_device_id) if active_device_id is not None else None
                    ),
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )

    async def satellite_state(
        self,
        connection: DeviceCommandConnection,
        frame: SatelliteStateFrame,
    ) -> None:
        if SATELLITE_CAPABILITY not in connection.capabilities:
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "satellite.error",
                    "reason_code": "capability_not_authorized",
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
            return
        event = device_reported_event(frame.state)
        session = self.satellites.get(connection.principal.device_id)
        if (
            session is not None
            and frame.state == SatelliteState.IDLE
            and session.state == SatelliteState.IDLE
        ):
            # 设备重申 idle 幂等接受，不产生错误帧
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "satellite.state.accepted",
                    "state": SatelliteState.IDLE.value,
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
            return
        try:
            if event is None:
                # idle→listening 只能由 Hub 仲裁触发；其余目标状态必须有等价事件
                raise InvalidSatelliteTransition(
                    session.state if session is not None else SatelliteState.IDLE,
                    SatelliteEvent.WAKE_ACCEPTED,
                )
            state = self.satellites.apply_event(connection.principal.device_id, event)
        except (LookupError, InvalidSatelliteTransition) as error:
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "satellite.error",
                    "reason_code": (
                        "not_registered" if isinstance(error, LookupError) else "illegal_transition"
                    ),
                    "detail": frame.reason_code,
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
            return
        await connection.send_signed(
            {
                "proto_version": 1,
                "type": "satellite.state.accepted",
                "state": state.value,
                "sent_at": datetime.now(UTC).isoformat(),
            }
        )

    async def satellite_audio_start(
        self,
        connection: DeviceCommandConnection,
        frame: SatelliteAudioStartFrame,
    ) -> None:
        device_id = connection.principal.device_id
        session = self.satellites.get(device_id)
        if SATELLITE_CAPABILITY not in connection.capabilities:
            await self._send_satellite_error(connection, "capability_not_authorized")
            return
        if session is None:
            await self._send_satellite_error(connection, "not_registered")
            return
        privacy_level = PrivacyLevel(frame.privacy_level)
        if _privacy_rank(privacy_level) > _privacy_rank(session.max_privacy_level):
            await self._send_satellite_error(connection, "privacy_level_not_allowed")
            return
        if session.state in {SatelliteState.PROCESSING, SatelliteState.SPEAKING} and frame.barge_in:
            task = self._satellite_tasks.pop(device_id, None)
            if task is not None:
                task.cancel()
            state = self.satellites.apply_event(device_id, SatelliteEvent.INTERRUPT)
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "satellite.interrupted",
                    "state": state.value,
                    "reason_code": "barge_in",
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
        if session.state is not SatelliteState.LISTENING:
            await self._send_satellite_error(connection, "not_listening")
            return
        if device_id in self._satellite_audio or device_id in self._satellite_tasks:
            await self._send_satellite_error(connection, "utterance_in_progress")
            return
        self._cancel_follow_up_timeout(device_id)
        session.follow_up_until = None
        self._satellite_audio[device_id] = SatelliteAudioUpload(
            utterance_id=frame.utterance_id,
            privacy_level=privacy_level,
        )
        await connection.send_signed(
            {
                "proto_version": 1,
                "type": "satellite.audio.accepted",
                "utterance_id": str(frame.utterance_id),
                "sent_at": datetime.now(UTC).isoformat(),
            }
        )

    async def satellite_audio_chunk(
        self,
        connection: DeviceCommandConnection,
        frame: SatelliteAudioChunkFrame,
    ) -> None:
        device_id = connection.principal.device_id
        if SATELLITE_CAPABILITY not in connection.capabilities:
            self._cancel_satellite(device_id)
            await self._send_satellite_error(connection, "capability_not_authorized")
            return
        upload = self._satellite_audio.get(device_id)
        if upload is None or upload.utterance_id != frame.utterance_id:
            await self._send_satellite_error(connection, "unknown_utterance")
            return
        if frame.index != upload.next_index:
            self._satellite_audio.pop(device_id, None)
            self.satellites.apply_event(device_id, SatelliteEvent.ERROR)
            await self._send_satellite_error(connection, "chunk_out_of_order")
            return
        try:
            chunk = base64.b64decode(frame.data_b64, validate=True)
        except (binascii.Error, ValueError):
            self._satellite_audio.pop(device_id, None)
            self.satellites.apply_event(device_id, SatelliteEvent.ERROR)
            await self._send_satellite_error(connection, "invalid_audio_base64")
            return
        if not chunk or len(upload.data) + len(chunk) > SATELLITE_AUDIO_MAX_BYTES:
            self._satellite_audio.pop(device_id, None)
            self.satellites.apply_event(device_id, SatelliteEvent.ERROR)
            await self._send_satellite_error(connection, "audio_too_large")
            return
        upload.data.extend(chunk)
        upload.next_index += 1

    async def satellite_audio_end(
        self,
        connection: DeviceCommandConnection,
        frame: SatelliteAudioEndFrame,
    ) -> None:
        device_id = connection.principal.device_id
        if SATELLITE_CAPABILITY not in connection.capabilities:
            self._cancel_satellite(device_id)
            await self._send_satellite_error(connection, "capability_not_authorized")
            return
        upload = self._satellite_audio.pop(device_id, None)
        if upload is None or upload.utterance_id != frame.utterance_id:
            await self._send_satellite_error(connection, "unknown_utterance")
            return
        data = bytes(upload.data)
        if (
            upload.next_index != frame.chunks
            or len(data) != frame.bytes
            or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), frame.sha256)
        ):
            self.satellites.apply_event(device_id, SatelliteEvent.ERROR)
            await self._send_satellite_error(connection, "audio_integrity_failed")
            return
        if len(data) < SATELLITE_AUDIO_MIN_BYTES:
            self.satellites.apply_event(device_id, SatelliteEvent.ERROR)
            await self._send_satellite_error(connection, "utterance_too_short")
            return
        if self._satellite_utterance_handler is None:
            self.satellites.apply_event(device_id, SatelliteEvent.ERROR)
            await self._send_satellite_error(connection, "voice_pipeline_unavailable")
            return
        state = self.satellites.apply_event(device_id, SatelliteEvent.UTTERANCE_END)
        await connection.send_signed(
            {
                "proto_version": 1,
                "type": "satellite.state.set",
                "state": state.value,
                "utterance_id": str(frame.utterance_id),
                "sent_at": datetime.now(UTC).isoformat(),
            }
        )
        task = asyncio.create_task(
            self._run_satellite_utterance(connection, upload, data),
            name=f"satellite-utterance-{frame.utterance_id}",
        )
        self._satellite_tasks[device_id] = task
        task.add_done_callback(
            lambda finished: self._finish_satellite_utterance(device_id, finished)
        )

    async def satellite_cancel(
        self,
        connection: DeviceCommandConnection,
        frame: SatelliteCancelFrame,
    ) -> None:
        device_id = connection.principal.device_id
        session = self.satellites.get(device_id)
        if SATELLITE_CAPABILITY not in connection.capabilities:
            await self._send_satellite_error(connection, "capability_not_authorized")
            return
        if session is None:
            await self._send_satellite_error(connection, "not_registered")
            return
        upload = self._satellite_audio.get(device_id)
        if (
            frame.utterance_id is not None
            and upload is not None
            and upload.utterance_id != frame.utterance_id
        ):
            await self._send_satellite_error(connection, "unknown_utterance")
            return
        self._satellite_audio.pop(device_id, None)
        task = self._satellite_tasks.pop(device_id, None)
        if task is not None:
            task.cancel()
        self._cancel_follow_up_timeout(device_id)
        state = self.satellites.apply_event(device_id, SatelliteEvent.CANCEL)
        await connection.send_signed(
            {
                "proto_version": 1,
                "type": "satellite.cancelled",
                "utterance_id": str(frame.utterance_id) if frame.utterance_id else None,
                "state": state.value,
                "reason_code": frame.reason_code,
                "sent_at": datetime.now(UTC).isoformat(),
            }
        )

    def _finish_satellite_utterance(
        self, device_id: UUID, task: asyncio.Task[None]
    ) -> None:
        if self._satellite_tasks.get(device_id) is task:
            self._satellite_tasks.pop(device_id, None)

    async def _run_satellite_utterance(
        self,
        connection: DeviceCommandConnection,
        upload: SatelliteAudioUpload,
        data: bytes,
    ) -> None:
        device_id = connection.principal.device_id
        current_task = asyncio.current_task()
        speaking = False

        async def emit(frame_type: str, payload: dict[str, JsonValue]) -> None:
            nonlocal speaking
            if frame_type == "voice.sentence" and not speaking:
                try:
                    state = self.satellites.apply_event(device_id, SatelliteEvent.REPLY_READY)
                except (LookupError, InvalidSatelliteTransition):
                    return
                speaking = True
                await connection.send_signed(
                    {
                        "proto_version": 1,
                        "type": "satellite.state.set",
                        "state": state.value,
                        "utterance_id": str(upload.utterance_id),
                        "sent_at": datetime.now(UTC).isoformat(),
                    }
                )
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": frame_type,
                    "utterance_id": str(upload.utterance_id),
                    **payload,
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )

        try:
            handler = self._satellite_utterance_handler
            assert handler is not None
            completed = await handler(
                connection.principal.owner_user_id,
                device_id,
                data,
                upload.privacy_level,
                emit,
            )
            # 打断/取消会先从任务表撤销旧回合；即使下游吞掉 CancelledError，
            # 旧回合也不能再覆盖新一轮 listening/processing 状态。
            if self._satellite_tasks.get(device_id) is not current_task:
                return
            session = self.satellites.get(device_id)
            if completed and session is not None and session.continuous_timeout_seconds > 0:
                state = self.satellites.apply_event(device_id, SatelliteEvent.FOLLOW_UP_READY)
                session.follow_up_until = datetime.now(UTC) + timedelta(
                    seconds=session.continuous_timeout_seconds
                )
                follow_up = True
                self._schedule_follow_up_timeout(connection, session.follow_up_until)
                await self._announce_follow_up_available(connection, session.follow_up_until)
            else:
                state = self.satellites.apply_event(device_id, SatelliteEvent.REPLY_DONE)
                follow_up = False
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "satellite.state.set",
                    "state": state.value,
                    "follow_up": follow_up,
                    "follow_up_until": (
                        session.follow_up_until.isoformat()
                        if follow_up and session is not None and session.follow_up_until
                        else None
                    ),
                    "utterance_id": str(upload.utterance_id),
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._satellite_tasks.get(device_id) is current_task:
                with suppress(LookupError):
                    self.satellites.apply_event(device_id, SatelliteEvent.ERROR)
                await self._send_satellite_error(connection, "voice_pipeline_failed")

    async def _announce_follow_up_available(
        self, source: DeviceCommandConnection, expires_at: datetime
    ) -> None:
        source_session = self.satellites.get(source.principal.device_id)
        if source_session is None:
            return
        targets = [
            connection
            for device_id, connection in self._connections.items()
            if device_id != source.principal.device_id
            and connection.principal.owner_user_id == source.principal.owner_user_id
            and (session := self.satellites.get(device_id)) is not None
            and session.state is SatelliteState.IDLE
        ]
        frame = {
            "proto_version": 1,
            "type": "satellite.session.available",
            "from_device_id": str(source.principal.device_id),
            "from_room_id": source_session.room_id,
            "follow_up_until": expires_at.isoformat(),
            "sent_at": datetime.now(UTC).isoformat(),
        }
        await asyncio.gather(
            *(connection.send_signed(frame) for connection in targets),
            return_exceptions=True,
        )

    def _schedule_follow_up_timeout(
        self, connection: DeviceCommandConnection, expires_at: datetime
    ) -> None:
        device_id = connection.principal.device_id
        self._cancel_follow_up_timeout(device_id)
        task = asyncio.create_task(
            self._expire_follow_up(connection, expires_at),
            name=f"satellite-follow-up-{device_id}",
        )
        self._satellite_follow_up_tasks[device_id] = task
        task.add_done_callback(
            lambda finished: self._finish_follow_up_timeout(device_id, finished)
        )

    def _finish_follow_up_timeout(
        self, device_id: UUID, task: asyncio.Task[None]
    ) -> None:
        if self._satellite_follow_up_tasks.get(device_id) is task:
            self._satellite_follow_up_tasks.pop(device_id, None)

    async def _expire_follow_up(
        self, connection: DeviceCommandConnection, expires_at: datetime
    ) -> None:
        delay = max(0.0, (expires_at - datetime.now(UTC)).total_seconds())
        await asyncio.sleep(delay)
        device_id = connection.principal.device_id
        session = self.satellites.get(device_id)
        if session is None or session.follow_up_until != expires_at:
            return
        self.satellites.apply_event(device_id, SatelliteEvent.CANCEL)
        await connection.send_signed(
            {
                "proto_version": 1,
                "type": "satellite.session.expired",
                "state": SatelliteState.IDLE.value,
                "sent_at": datetime.now(UTC).isoformat(),
            }
        )

    async def satellite_takeover(
        self,
        connection: DeviceCommandConnection,
        frame: SatelliteTakeoverFrame,
    ) -> None:
        target_id = connection.principal.device_id
        target = self.satellites.get(target_id)
        source = self.satellites.get(frame.from_device_id)
        if SATELLITE_CAPABILITY not in connection.capabilities:
            await self._send_satellite_error(connection, "capability_not_authorized")
            return
        now = datetime.now(UTC)
        if target is None or source is None:
            await self._send_satellite_error(connection, "not_registered")
            return
        if source.owner_user_id != connection.principal.owner_user_id:
            await self._send_satellite_error(connection, "takeover_not_allowed")
            return
        if (
            target.state is not SatelliteState.IDLE
            or source.state is not SatelliteState.LISTENING
            or source.follow_up_until is None
            or source.follow_up_until <= now
        ):
            await self._send_satellite_error(connection, "takeover_not_available")
            return
        expires_at = source.follow_up_until
        if self._satellite_takeover_handler is not None:
            try:
                await self._satellite_takeover_handler(
                    connection.principal.owner_user_id,
                    source.device_id,
                    target.device_id,
                )
            except Exception:
                await self._send_satellite_error(connection, "takeover_context_failed")
                return
        self._cancel_follow_up_timeout(source.device_id)
        self.satellites.apply_event(source.device_id, SatelliteEvent.CANCEL)
        self.satellites.apply_event(target.device_id, SatelliteEvent.WAKE_ACCEPTED)
        target.follow_up_until = expires_at
        self._schedule_follow_up_timeout(connection, expires_at)
        source_connection = self._connections.get(source.device_id)
        if source_connection is not None:
            await source_connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "satellite.session.transferred",
                    "to_device_id": str(target.device_id),
                    "state": SatelliteState.IDLE.value,
                    "sent_at": now.isoformat(),
                }
            )
        await connection.send_signed(
            {
                "proto_version": 1,
                "type": "satellite.session.accepted",
                "from_device_id": str(source.device_id),
                "state": SatelliteState.LISTENING.value,
                "follow_up_until": expires_at.isoformat(),
                "sent_at": now.isoformat(),
            }
        )

    @staticmethod
    async def _send_satellite_error(
        connection: DeviceCommandConnection, reason_code: str
    ) -> None:
        await connection.send_signed(
            {
                "proto_version": 1,
                "type": "satellite.error",
                "reason_code": reason_code,
                "sent_at": datetime.now(UTC).isoformat(),
            }
        )

    async def start_pet_message(
        self, connection: DeviceCommandConnection, frame: PetMessageFrame
    ) -> None:
        key = (connection.principal.device_id, frame.request_id)
        if "avatar.chat" not in connection.capabilities:
            await self._send_pet_message_failure(
                connection, frame.request_id, "capability_not_authorized"
            )
            return
        if self._pet_message_handler is None:
            await self._send_pet_message_failure(
                connection, frame.request_id, "pet_chat_unavailable"
            )
            return
        if key in self._pet_message_requests:
            await self._send_pet_message_failure(connection, frame.request_id, "duplicate_request")
            return
        if any(device_id == key[0] for device_id, _ in self._pet_message_tasks):
            await self._send_pet_message_failure(connection, frame.request_id, "turn_in_progress")
            return
        previous_requests = [
            request_key
            for request_key in self._pet_message_requests
            if request_key[0] == key[0] and request_key != key
        ]
        if len(previous_requests) >= 200:
            self._pet_message_requests.discard(previous_requests[0])
        self._pet_message_requests.add(key)
        await connection.send_signed(
            {
                "proto_version": 1,
                "type": "pet.message.accepted",
                "request_id": str(frame.request_id),
                "sent_at": datetime.now(UTC).isoformat(),
            }
        )
        task = asyncio.create_task(
            self._run_pet_message(connection, frame),
            name=f"pet-message-{frame.request_id}",
        )
        self._pet_message_tasks[key] = task
        task.add_done_callback(lambda finished: self._finish_pet_message(key, finished))

    def _finish_pet_message(self, key: tuple[UUID, UUID], task: asyncio.Task[None]) -> None:
        if self._pet_message_tasks.get(key) is task:
            self._pet_message_tasks.pop(key, None)

    async def _run_pet_message(
        self, connection: DeviceCommandConnection, frame: PetMessageFrame
    ) -> None:
        try:
            handler = self._pet_message_handler
            if handler is None:
                await self._send_pet_message_failure(
                    connection, frame.request_id, "pet_chat_unavailable"
                )
                return
            result, speech_text = await handler(
                connection.principal.owner_user_id,
                frame.text.strip(),
                PrivacyLevel(frame.privacy_level),
            )
            audio_delivered = False
            if frame.speak:
                audio_delivered = await self._stream_pet_audio(
                    connection,
                    frame.request_id,
                    speech_text,
                    PrivacyLevel(frame.privacy_level),
                )
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "pet.message.completed",
                    "request_id": str(frame.request_id),
                    "result": {
                        **result,
                        "audio_requested": frame.speak,
                        "audio_delivered": audio_delivered,
                    },
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._send_pet_message_failure(connection, frame.request_id, "generation_failed")

    async def _stream_pet_audio(
        self,
        connection: DeviceCommandConnection,
        request_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
    ) -> bool:
        async def emit_audio(frame_type: str, payload: dict[str, JsonValue]) -> None:
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": frame_type,
                    "request_id": str(request_id),
                    **payload,
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )

        handler = self._pet_audio_handler
        if handler is None:
            await emit_audio("pet.audio.failed", {"reason_code": "tts_not_configured"})
            return False
        try:
            return await handler(text, privacy_level, emit_audio)
        except asyncio.CancelledError:
            raise
        except Exception:
            await emit_audio("pet.audio.failed", {"reason_code": "tts_generation_failed"})
            return False

    async def _send_pet_message_failure(
        self, connection: DeviceCommandConnection, request_id: UUID, reason_code: str
    ) -> None:
        with suppress(Exception):
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "pet.message.failed",
                    "request_id": str(request_id),
                    "reason_code": reason_code,
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )

    async def publish_avatar_control(
        self, owner_user_id: UUID, control: dict[str, JsonValue]
    ) -> int:
        if not control:
            return 0
        connections = [
            connection
            for connection in self._connections.values()
            if connection.principal.owner_user_id == owner_user_id
            and "avatar.render" in connection.capabilities
        ]
        if not connections:
            return 0
        sequence = self._avatar_sequences.get(owner_user_id, 0) + 1
        self._avatar_sequences[owner_user_id] = sequence
        frame = {
            "proto_version": 1,
            "type": "avatar.control",
            "sequence": sequence,
            "sent_at": datetime.now(UTC).isoformat(),
            "control": control,
        }
        results = await asyncio.gather(
            *(connection.send_signed(frame) for connection in connections),
            return_exceptions=True,
        )
        delivered = 0
        for connection, result in zip(connections, results, strict=True):
            if isinstance(result, Exception):
                self.disconnect(connection)
            else:
                delivered += 1
        return delivered

    async def broadcast_satellite(
        self,
        owner_user_id: UUID,
        text: str,
        *,
        privacy_level: PrivacyLevel,
        room_id: str | None = None,
        emergency: bool = False,
    ) -> int:
        """普通播报选一个空闲房间端；紧急播报覆盖全部符合隐私级别的空闲端。"""
        if self._pet_audio_handler is None:
            return 0
        candidates = []
        for session in self.satellites.sessions.values():
            connection = self._connections.get(session.device_id)
            if (
                connection is None
                or session.owner_user_id != owner_user_id
                or session.state is not SatelliteState.IDLE
                or (room_id is not None and session.room_id != room_id)
                or _privacy_rank(privacy_level) > _privacy_rank(session.max_privacy_level)
            ):
                continue
            candidates.append((session.room_id, str(session.device_id), connection))
        candidates.sort(key=lambda item: (item[0], item[1]))
        if not emergency:
            candidates = candidates[:1]
        results = await asyncio.gather(
            *(
                self._broadcast_satellite_one(
                    connection,
                    text,
                    privacy_level=privacy_level,
                    emergency=emergency,
                )
                for _, _, connection in candidates
            ),
            return_exceptions=True,
        )
        return sum(result is True for result in results)

    async def _broadcast_satellite_one(
        self,
        connection: DeviceCommandConnection,
        text: str,
        *,
        privacy_level: PrivacyLevel,
        emergency: bool,
    ) -> bool:
        device_id = connection.principal.device_id
        try:
            state = self.satellites.apply_event(device_id, SatelliteEvent.BROADCAST_READY)
        except (LookupError, InvalidSatelliteTransition):
            return False
        broadcast_id = uuid7()

        async def emit(frame_type: str, payload: dict[str, JsonValue]) -> None:
            mapped_type = frame_type.replace("pet.audio.", "satellite.broadcast.audio.")
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": mapped_type,
                    "broadcast_id": str(broadcast_id),
                    **payload,
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )

        try:
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "satellite.state.set",
                    "state": state.value,
                    "broadcast_id": str(broadcast_id),
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "satellite.broadcast",
                    "broadcast_id": str(broadcast_id),
                    "text": text,
                    "privacy_level": privacy_level.value,
                    "emergency": emergency,
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
            handler = self._pet_audio_handler
            assert handler is not None
            delivered = await handler(text, privacy_level, emit)
            return delivered
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        finally:
            with suppress(LookupError, InvalidSatelliteTransition):
                state = self.satellites.apply_event(device_id, SatelliteEvent.REPLY_DONE)
                await connection.send_signed(
                    {
                        "proto_version": 1,
                        "type": "satellite.state.set",
                        "state": state.value,
                        "broadcast_id": str(broadcast_id),
                        "sent_at": datetime.now(UTC).isoformat(),
                    }
                )

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
        # A newer Hub must not send snapshot-bound operations to an older extension
        # that would silently ignore snapshot_id and still use positional refs.
        if command in {"browser.form.read", "browser.form.fill", "browser.form.submit"} and (
            "browser.form.snapshot_v1" not in device.capabilities
        ):
            result = await self._store.mark_delivery_failed(
                issued.command.id, reason_code="browser_snapshot_upgrade_required"
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
        if (
            result_meta is not None
            and len(json.dumps(result_meta, ensure_ascii=False, separators=(",", ":")).encode())
            > 4096
        ):
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
    ) -> DeviceSnapshot:
        return await self._registry.heartbeat(principal, capabilities=capabilities)

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
            _command_response(value) for value in await store.list(device_id=device_id, limit=limit)
        ]

    @admin.get("/api/v1/admin/device-commands/{command_id}", response_model=CommandResponse)
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
            device = await registry.heartbeat(principal, capabilities=tuple(auth.capabilities))
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
            capabilities=device.effective_capabilities,
        )
        await gateway.connect(connection)
        await connection.send_signed(
            {
                "proto_version": 1,
                "type": "device.accepted",
                "device_id": str(principal.device_id),
                "heartbeat_interval_seconds": 30,
                "observe_tab_hint": gateway.tab_hint_enabled,
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
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid content length")
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
            device = await gateway.heartbeat(
                connection.principal, tuple(heartbeat_frame.capabilities)
            )
            connection.capabilities = device.effective_capabilities
            if (
                gateway.tab_hint_enabled
                and heartbeat_frame.tab_hint is not None
                and "browser.current_tab.read" in connection.capabilities
            ):
                gateway.update_tab_hint(
                    connection.principal.device_id,
                    origin=heartbeat_frame.tab_hint.origin,
                    title=heartbeat_frame.tab_hint.title,
                )
            await connection.send_signed(
                {
                    "proto_version": 1,
                    "type": "heartbeat.accepted",
                    "observe_tab_hint": gateway.tab_hint_enabled,
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
        elif frame_type == "pet.message.send":
            await gateway.start_pet_message(connection, PetMessageFrame.model_validate(raw))
        elif frame_type == "satellite.hello":
            await gateway.satellite_hello(connection, SatelliteHelloFrame.model_validate(raw))
        elif frame_type == "satellite.wake":
            await gateway.satellite_wake(connection, SatelliteWakeFrame.model_validate(raw))
        elif frame_type == "satellite.state":
            await gateway.satellite_state(connection, SatelliteStateFrame.model_validate(raw))
        elif frame_type == "satellite.audio.start":
            await gateway.satellite_audio_start(
                connection, SatelliteAudioStartFrame.model_validate(raw)
            )
        elif frame_type == "satellite.audio.chunk":
            await gateway.satellite_audio_chunk(
                connection, SatelliteAudioChunkFrame.model_validate(raw)
            )
        elif frame_type == "satellite.audio.end":
            await gateway.satellite_audio_end(
                connection, SatelliteAudioEndFrame.model_validate(raw)
            )
        elif frame_type == "satellite.cancel":
            await gateway.satellite_cancel(connection, SatelliteCancelFrame.model_validate(raw))
        elif frame_type == "satellite.takeover":
            await gateway.satellite_takeover(
                connection, SatelliteTakeoverFrame.model_validate(raw)
            )
        elif frame_type == "command.ack":
            ack_frame = CommandAckFrame.model_validate(raw)
            result = await gateway.acknowledge(connection.principal.device_id, ack_frame.command_id)
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


def _privacy_rank(level: PrivacyLevel) -> int:
    return {PrivacyLevel.L0: 0, PrivacyLevel.L1: 1, PrivacyLevel.L2: 2}[level]


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
