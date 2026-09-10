from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest

from app.api.device_commands import DeviceCommandConnection, DeviceCommandGateway
from app.db import create_database
from app.devices import DeviceCommandStore, DevicePrincipal, DeviceRegistry
from app.ids import uuid7
from app.satellite import (
    SATELLITE_CAPABILITY,
    InvalidSatelliteTransition,
    SatelliteAudioChunkFrame,
    SatelliteAudioEndFrame,
    SatelliteAudioStartFrame,
    SatelliteCancelFrame,
    SatelliteEvent,
    SatelliteRegistry,
    SatelliteState,
    next_state,
)
from app.schemas import PrivacyLevel

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


def test_legal_lifecycle_transitions() -> None:
    state = SatelliteState.IDLE
    for event, expected in [
        (SatelliteEvent.WAKE_ACCEPTED, SatelliteState.LISTENING),
        (SatelliteEvent.UTTERANCE_END, SatelliteState.PROCESSING),
        (SatelliteEvent.REPLY_READY, SatelliteState.SPEAKING),
        (SatelliteEvent.REPLY_DONE, SatelliteState.IDLE),
    ]:
        state = next_state(state, event)
        assert state is expected


def test_cancel_and_error_return_to_idle_from_any_state() -> None:
    for start in SatelliteState:
        assert next_state(start, SatelliteEvent.CANCEL) is SatelliteState.IDLE
        assert next_state(start, SatelliteEvent.ERROR) is SatelliteState.IDLE


def test_illegal_transitions_rejected() -> None:
    with pytest.raises(InvalidSatelliteTransition):
        next_state(SatelliteState.IDLE, SatelliteEvent.UTTERANCE_END)  # 未唤醒不能结束语句
    with pytest.raises(InvalidSatelliteTransition):
        next_state(SatelliteState.IDLE, SatelliteEvent.REPLY_READY)  # 不能跳过听音
    with pytest.raises(InvalidSatelliteTransition):
        next_state(SatelliteState.SPEAKING, SatelliteEvent.WAKE_ACCEPTED)  # 播报中不能再唤醒


# ---------------------------------------------------------------------------
# 注册表与唤醒仲裁（SAT-02 核心）
# ---------------------------------------------------------------------------


def _registry_with(owner: UUID, *device_ids: UUID) -> SatelliteRegistry:
    registry = SatelliteRegistry()
    for device_id in device_ids:
        registry.register(device_id=device_id, owner_user_id=owner, room_id="客厅")
    return registry


async def test_wake_first_wins_second_suppressed_within_window() -> None:
    owner = uuid7()
    first, second = uuid7(), uuid7()
    registry = _registry_with(owner, first, second)

    decision_one = registry.arbitrate_wake(device_id=first, owner_user_id=owner, now=NOW)
    decision_two = registry.arbitrate_wake(device_id=second, owner_user_id=owner, now=NOW)

    assert decision_one.winner and decision_one.session_state is SatelliteState.LISTENING
    assert not decision_two.winner
    assert decision_two.reason_code == "session_active"


async def test_wake_window_suppresses_after_session_ends() -> None:
    owner = uuid7()
    first, second = uuid7(), uuid7()
    registry = _registry_with(owner, first, second)

    assert registry.arbitrate_wake(device_id=first, owner_user_id=owner, now=NOW).winner
    # 会话结束（listening → idle）
    registry.apply_event(first, SatelliteEvent.CANCEL)
    # 窗口内另一个卫星的唤醒仍被压制
    blocked = registry.arbitrate_wake(
        device_id=second, owner_user_id=owner, now=NOW + timedelta(seconds=1)
    )
    assert not blocked.winner
    assert blocked.reason_code == "arbitration_window"
    # 窗口过后可再次唤醒
    allowed = registry.arbitrate_wake(
        device_id=second, owner_user_id=owner, now=NOW + timedelta(seconds=3)
    )
    assert allowed.winner


async def test_unregister_removes_session_and_counters() -> None:
    owner = uuid7()
    device = uuid7()
    registry = _registry_with(owner, device)
    registry.arbitrate_wake(device_id=device, owner_user_id=owner, now=NOW)
    registry.unregister(device)
    assert registry.get(device) is None
    decision = registry.arbitrate_wake(device_id=device, owner_user_id=owner, now=NOW)
    assert not decision.winner
    assert decision.reason_code == "not_registered"


async def test_reregister_resets_state_but_keeps_counters() -> None:
    owner = uuid7()
    device = uuid7()
    registry = _registry_with(owner, device)
    registry.arbitrate_wake(device_id=device, owner_user_id=owner, now=NOW)
    registry.register(device_id=device, owner_user_id=owner, room_id="卧室")
    session = registry.get(device)
    assert session is not None
    assert session.state is SatelliteState.IDLE  # 重连从 idle 开始
    assert session.room_id == "卧室"
    assert session.wake_count == 1  # 计数保留用于观测


# ---------------------------------------------------------------------------
# 网关帧处理
# ---------------------------------------------------------------------------


class FakeWebSocket:
    def __init__(self) -> None:
        self.frames: list[dict[str, object]] = []

    async def send_json(self, frame: dict[str, object]) -> None:
        self.frames.append(frame)

    async def close(self, **_: object) -> None:
        return None


def _gateway() -> DeviceCommandGateway:
    database = create_database("sqlite+aiosqlite:///:memory:")
    return DeviceCommandGateway(DeviceRegistry(database), DeviceCommandStore(database))


def _connection(
    gateway: DeviceCommandGateway,
    owner: UUID,
    *,
    capabilities: tuple[str, ...] = (SATELLITE_CAPABILITY,),
) -> tuple[FakeWebSocket, DeviceCommandConnection]:
    websocket = FakeWebSocket()
    connection = DeviceCommandConnection(
        websocket=cast(Any, websocket),
        principal=DevicePrincipal(device_id=uuid7(), owner_user_id=owner),
        access_token="aria-device-satellite-secret",
        capabilities=capabilities,
    )
    return websocket, connection


async def test_gateway_hello_wake_and_state_flow() -> None:
    gateway = _gateway()
    owner = uuid7()
    websocket, connection = _connection(gateway, owner)
    await gateway.connect(connection)

    from app.satellite import SatelliteHelloFrame, SatelliteStateFrame, SatelliteWakeFrame

    await gateway.satellite_hello(
        connection, SatelliteHelloFrame(room_id="客厅", firmware="esp32-v1")
    )
    ready = websocket.frames[-1]
    assert str(ready["type"]) == "satellite.ready"
    assert str(ready["state"]) == "idle"

    await gateway.satellite_wake(
        connection, SatelliteWakeFrame(room_id="客厅", detected_at=datetime.now(UTC))
    )
    listening = websocket.frames[-1]
    assert str(listening["type"]) == "satellite.state.set"
    assert str(listening["state"]) == "listening"

    await gateway.satellite_state(connection, SatelliteStateFrame(state=SatelliteState.PROCESSING))
    processing = websocket.frames[-1]
    assert str(processing["type"]) == "satellite.state.accepted"
    assert str(processing["state"]) == "processing"

    await gateway.satellite_state(connection, SatelliteStateFrame(state=SatelliteState.SPEAKING))
    await gateway.satellite_state(connection, SatelliteStateFrame(state=SatelliteState.IDLE))
    assert websocket.frames[-1]["state"] == "idle"


async def test_gateway_second_satellite_wake_suppressed() -> None:
    gateway = _gateway()
    owner = uuid7()
    from app.satellite import SatelliteHelloFrame, SatelliteWakeFrame

    ws_one, conn_one = _connection(gateway, owner)
    ws_two, conn_two = _connection(gateway, owner)
    await gateway.connect(conn_one)
    await gateway.connect(conn_two)
    await gateway.satellite_hello(conn_one, SatelliteHelloFrame(room_id="客厅"))
    await gateway.satellite_hello(conn_two, SatelliteHelloFrame(room_id="卧室"))

    await gateway.satellite_wake(
        conn_one, SatelliteWakeFrame(room_id="客厅", detected_at=datetime.now(UTC))
    )
    await gateway.satellite_wake(
        conn_two, SatelliteWakeFrame(room_id="卧室", detected_at=datetime.now(UTC))
    )
    assert ws_one.frames[-1]["type"] == "satellite.state.set"
    assert ws_two.frames[-1]["type"] == "satellite.wake.suppressed"
    assert ws_two.frames[-1]["reason_code"] == "session_active"


async def test_gateway_satellite_requires_capability() -> None:
    gateway = _gateway()
    owner = uuid7()
    from app.satellite import SatelliteHelloFrame

    websocket, connection = _connection(gateway, owner, capabilities=("screen.capture",))
    await gateway.connect(connection)
    await gateway.satellite_hello(connection, SatelliteHelloFrame(room_id="客厅"))
    assert websocket.frames[-1]["type"] == "satellite.error"
    assert websocket.frames[-1]["reason_code"] == "capability_not_authorized"


async def test_gateway_rejects_device_driven_listening_and_illegal_jump() -> None:
    gateway = _gateway()
    owner = uuid7()
    from app.satellite import SatelliteHelloFrame, SatelliteStateFrame, SatelliteWakeFrame

    websocket, connection = _connection(gateway, owner)
    await gateway.connect(connection)
    await gateway.satellite_hello(connection, SatelliteHelloFrame(room_id="客厅"))

    # 设备不能自行上报 listening（必须经 Hub 仲裁）
    await gateway.satellite_state(connection, SatelliteStateFrame(state=SatelliteState.LISTENING))
    assert websocket.frames[-1]["type"] == "satellite.error"
    assert websocket.frames[-1]["reason_code"] == "illegal_transition"

    # 合法唤醒后，从 listening 直接跳 speaking 也非法
    await gateway.satellite_wake(
        connection, SatelliteWakeFrame(room_id="客厅", detected_at=datetime.now(UTC))
    )
    await gateway.satellite_state(connection, SatelliteStateFrame(state=SatelliteState.SPEAKING))
    assert websocket.frames[-1]["reason_code"] == "illegal_transition"

    # 未注册设备的上报
    other_ws, other_conn = _connection(gateway, owner)
    await gateway.connect(other_conn)
    await gateway.satellite_state(other_conn, SatelliteStateFrame(state=SatelliteState.PROCESSING))
    assert other_ws.frames[-1]["reason_code"] == "not_registered"


async def test_gateway_idle_reassert_is_idempotent() -> None:
    gateway = _gateway()
    owner = uuid7()
    from app.satellite import SatelliteHelloFrame, SatelliteStateFrame

    websocket, connection = _connection(gateway, owner)
    await gateway.connect(connection)
    await gateway.satellite_hello(connection, SatelliteHelloFrame(room_id="客厅"))
    await gateway.satellite_state(connection, SatelliteStateFrame(state=SatelliteState.IDLE))
    assert websocket.frames[-1]["type"] == "satellite.state.accepted"
    assert websocket.frames[-1]["state"] == "idle"


async def test_disconnect_unregisters_satellite() -> None:
    gateway = _gateway()
    owner = uuid7()
    from app.satellite import SatelliteHelloFrame

    _, connection = _connection(gateway, owner)
    await gateway.connect(connection)
    await gateway.satellite_hello(connection, SatelliteHelloFrame(room_id="客厅"))
    assert gateway.satellites.get(connection.principal.device_id) is not None
    gateway.disconnect(connection)
    assert gateway.satellites.get(connection.principal.device_id) is None


async def test_satellite_audio_runs_voice_pipeline_and_returns_to_idle() -> None:
    gateway = _gateway()
    owner = uuid7()
    websocket, connection = _connection(gateway, owner)
    await gateway.connect(connection)
    from app.satellite import SatelliteHelloFrame, SatelliteWakeFrame

    await gateway.satellite_hello(
        connection, SatelliteHelloFrame(room_id="书房", continuous_timeout_seconds=0)
    )
    await gateway.satellite_wake(
        connection, SatelliteWakeFrame(room_id="书房", detected_at=datetime.now(UTC))
    )
    utterance_id = uuid7()
    pcm = b"\x01\x00" * 2_500
    calls: list[tuple[UUID, UUID, bytes, PrivacyLevel]] = []

    async def handle(
        user_id: UUID,
        device_id: UUID,
        data: bytes,
        privacy: PrivacyLevel,
        emit: Any,
    ) -> bool:
        calls.append((user_id, device_id, data, privacy))
        await emit("voice.transcript", {"text": "你好", "is_final": True})
        await emit("voice.sentence", {"index": 0, "text": "你好", "mime": "audio/mpeg"})
        await emit("satellite.audio.chunk", {"index": 0, "data_b64": "AQI="})
        await emit("reply.committed", {"content": "你好"})
        return True

    gateway.set_satellite_utterance_handler(handle)
    await gateway.satellite_audio_start(
        connection,
        SatelliteAudioStartFrame(utterance_id=utterance_id, privacy_level="L1"),
    )
    await gateway.satellite_audio_chunk(
        connection,
        SatelliteAudioChunkFrame(
            utterance_id=utterance_id,
            index=0,
            data_b64=base64.b64encode(pcm).decode("ascii"),
        ),
    )
    await gateway.satellite_audio_end(
        connection,
        SatelliteAudioEndFrame(
            utterance_id=utterance_id,
            chunks=1,
            bytes=len(pcm),
            sha256=hashlib.sha256(pcm).hexdigest(),
        ),
    )
    task = gateway._satellite_tasks[connection.principal.device_id]
    await task

    assert calls == [(owner, connection.principal.device_id, pcm, PrivacyLevel.L1)]
    session = gateway.satellites.get(connection.principal.device_id)
    assert session is not None and session.state is SatelliteState.IDLE
    types = [frame["type"] for frame in websocket.frames]
    assert "voice.transcript" in types
    assert "satellite.audio.chunk" in types
    states = [
        frame["state"]
        for frame in websocket.frames
        if frame["type"] == "satellite.state.set"
    ]
    assert states == [
        "listening",
        "processing",
        "speaking",
        "idle",
    ]


async def test_satellite_audio_rejects_out_of_order_chunk_and_resets() -> None:
    gateway = _gateway()
    owner = uuid7()
    websocket, connection = _connection(gateway, owner)
    await gateway.connect(connection)
    from app.satellite import SatelliteHelloFrame, SatelliteWakeFrame

    await gateway.satellite_hello(connection, SatelliteHelloFrame(room_id="客厅"))
    await gateway.satellite_wake(
        connection, SatelliteWakeFrame(room_id="客厅", detected_at=datetime.now(UTC))
    )
    utterance_id = uuid7()
    await gateway.satellite_audio_start(
        connection, SatelliteAudioStartFrame(utterance_id=utterance_id)
    )
    await gateway.satellite_audio_chunk(
        connection,
        SatelliteAudioChunkFrame(utterance_id=utterance_id, index=1, data_b64="AQI="),
    )

    assert websocket.frames[-1]["reason_code"] == "chunk_out_of_order"
    session = gateway.satellites.get(connection.principal.device_id)
    assert session is not None and session.state is SatelliteState.IDLE


async def test_satellite_cancel_discards_partial_audio() -> None:
    gateway = _gateway()
    owner = uuid7()
    websocket, connection = _connection(gateway, owner)
    await gateway.connect(connection)
    from app.satellite import SatelliteHelloFrame, SatelliteWakeFrame

    await gateway.satellite_hello(connection, SatelliteHelloFrame(room_id="客厅"))
    await gateway.satellite_wake(
        connection, SatelliteWakeFrame(room_id="客厅", detected_at=datetime.now(UTC))
    )
    utterance_id = uuid7()
    await gateway.satellite_audio_start(
        connection, SatelliteAudioStartFrame(utterance_id=utterance_id)
    )
    await gateway.satellite_cancel(
        connection, SatelliteCancelFrame(utterance_id=utterance_id, reason_code="vad_cancelled")
    )

    assert connection.principal.device_id not in gateway._satellite_audio
    session = gateway.satellites.get(connection.principal.device_id)
    assert session is not None and session.state is SatelliteState.IDLE
    assert websocket.frames[-1]["type"] == "satellite.cancelled"


async def test_satellite_broadcast_routes_normal_and_emergency_by_room_and_privacy() -> None:
    gateway = _gateway()
    owner = uuid7()
    living_ws, living = _connection(gateway, owner)
    bedroom_ws, bedroom = _connection(gateway, owner)
    await gateway.connect(living)
    await gateway.connect(bedroom)
    from app.satellite import SatelliteHelloFrame

    await gateway.satellite_hello(
        living, SatelliteHelloFrame(room_id="客厅", max_privacy_level="L1")
    )
    await gateway.satellite_hello(
        bedroom, SatelliteHelloFrame(room_id="卧室", max_privacy_level="L2")
    )

    async def speak(_text: str, _privacy: PrivacyLevel, emit: Any) -> bool:
        await emit("pet.audio.start", {"mime": "audio/mpeg", "sample_rate": 24000})
        await emit("pet.audio.chunk", {"index": 0, "data_b64": "AQI="})
        await emit("pet.audio.end", {"chunks": 1, "bytes": 2})
        return True

    gateway.set_pet_audio_handler(speak)
    assert (
        await gateway.broadcast_satellite(
            owner, "卧室提醒", privacy_level=PrivacyLevel.L2, room_id="卧室"
        )
        == 1
    )
    assert any(frame["type"] == "satellite.broadcast" for frame in bedroom_ws.frames)
    assert not any(frame["type"] == "satellite.broadcast" for frame in living_ws.frames)

    assert (
        await gateway.broadcast_satellite(
            owner, "紧急提醒", privacy_level=PrivacyLevel.L1, emergency=True
        )
        == 2
    )
    assert sum(frame["type"] == "satellite.broadcast" for frame in living_ws.frames) == 1
    assert sum(frame["type"] == "satellite.broadcast" for frame in bedroom_ws.frames) == 2
    await asyncio.sleep(0)


async def _submit_satellite_pcm(
    gateway: DeviceCommandGateway,
    connection: DeviceCommandConnection,
    *,
    barge_in: bool = False,
) -> None:
    utterance_id = uuid7()
    pcm = b"\x02\x00" * 2_500
    await gateway.satellite_audio_start(
        connection,
        SatelliteAudioStartFrame(utterance_id=utterance_id, barge_in=barge_in),
    )
    await gateway.satellite_audio_chunk(
        connection,
        SatelliteAudioChunkFrame(
            utterance_id=utterance_id,
            index=0,
            data_b64=base64.b64encode(pcm).decode("ascii"),
        ),
    )
    await gateway.satellite_audio_end(
        connection,
        SatelliteAudioEndFrame(
            utterance_id=utterance_id,
            chunks=1,
            bytes=len(pcm),
            sha256=hashlib.sha256(pcm).hexdigest(),
        ),
    )


async def test_follow_up_accepts_second_utterance_without_wake() -> None:
    gateway = _gateway()
    owner = uuid7()
    websocket, connection = _connection(gateway, owner)
    await gateway.connect(connection)
    from app.satellite import SatelliteHelloFrame, SatelliteWakeFrame

    await gateway.satellite_hello(
        connection, SatelliteHelloFrame(room_id="书房", continuous_timeout_seconds=30)
    )
    await gateway.satellite_wake(
        connection, SatelliteWakeFrame(room_id="书房", detected_at=datetime.now(UTC))
    )
    turns = 0

    async def handle(*args: Any) -> bool:
        nonlocal turns
        emit = args[-1]
        turns += 1
        await emit("voice.sentence", {"index": 0, "text": f"回复{turns}"})
        await emit("reply.committed", {"content": f"回复{turns}"})
        return True

    gateway.set_satellite_utterance_handler(handle)
    await _submit_satellite_pcm(gateway, connection)
    await gateway._satellite_tasks[connection.principal.device_id]
    first_session = gateway.satellites.get(connection.principal.device_id)
    assert first_session is not None
    assert first_session.state is SatelliteState.LISTENING
    assert first_session.follow_up_until is not None

    await _submit_satellite_pcm(gateway, connection)
    await gateway._satellite_tasks[connection.principal.device_id]
    assert turns == 2
    assert not any(frame["type"] == "satellite.wake.suppressed" for frame in websocket.frames)
    await gateway.satellite_cancel(connection, SatelliteCancelFrame(reason_code="test_done"))


async def test_follow_up_timeout_returns_to_idle() -> None:
    gateway = _gateway()
    owner = uuid7()
    websocket, connection = _connection(gateway, owner)
    await gateway.connect(connection)
    from app.satellite import SatelliteHelloFrame, SatelliteWakeFrame

    await gateway.satellite_hello(
        connection, SatelliteHelloFrame(room_id="书房", continuous_timeout_seconds=0.01)
    )
    await gateway.satellite_wake(
        connection, SatelliteWakeFrame(room_id="书房", detected_at=datetime.now(UTC))
    )

    async def handle(*args: Any) -> bool:
        emit = args[-1]
        await emit("reply.committed", {"content": "完成"})
        return True

    gateway.set_satellite_utterance_handler(handle)
    await _submit_satellite_pcm(gateway, connection)
    await gateway._satellite_tasks[connection.principal.device_id]
    await asyncio.sleep(0.03)

    session = gateway.satellites.get(connection.principal.device_id)
    assert session is not None and session.state is SatelliteState.IDLE
    assert websocket.frames[-1]["type"] == "satellite.session.expired"


async def test_barge_in_cancels_speaking_turn_and_starts_new_utterance() -> None:
    gateway = _gateway()
    owner = uuid7()
    websocket, connection = _connection(gateway, owner)
    await gateway.connect(connection)
    from app.satellite import SatelliteHelloFrame, SatelliteWakeFrame

    await gateway.satellite_hello(connection, SatelliteHelloFrame(room_id="客厅"))
    await gateway.satellite_wake(
        connection, SatelliteWakeFrame(room_id="客厅", detected_at=datetime.now(UTC))
    )
    speaking = asyncio.Event()

    async def handle(*args: Any) -> bool:
        emit = args[-1]
        await emit("voice.sentence", {"index": 0, "text": "很长的回复"})
        speaking.set()
        await asyncio.Event().wait()
        return True

    gateway.set_satellite_utterance_handler(handle)
    await _submit_satellite_pcm(gateway, connection)
    await asyncio.wait_for(speaking.wait(), timeout=1)
    await _submit_satellite_pcm(gateway, connection, barge_in=True)
    await asyncio.sleep(0)

    assert any(frame["type"] == "satellite.interrupted" for frame in websocket.frames)
    assert any(
        frame["type"] == "satellite.audio.accepted"
        for frame in websocket.frames
    )
    current = gateway.satellites.get(connection.principal.device_id)
    assert current is not None and current.state is SatelliteState.SPEAKING
    await gateway.satellite_cancel(connection, SatelliteCancelFrame(reason_code="test_done"))


async def test_follow_up_can_transfer_to_another_room_for_same_owner() -> None:
    gateway = _gateway()
    owner = uuid7()
    source_ws, source = _connection(gateway, owner)
    target_ws, target = _connection(gateway, owner)
    await gateway.connect(source)
    await gateway.connect(target)
    from app.satellite import SatelliteHelloFrame, SatelliteTakeoverFrame, SatelliteWakeFrame

    await gateway.satellite_hello(
        source, SatelliteHelloFrame(room_id="客厅", continuous_timeout_seconds=30)
    )
    await gateway.satellite_hello(
        target, SatelliteHelloFrame(room_id="卧室", continuous_timeout_seconds=30)
    )
    await gateway.satellite_wake(
        source, SatelliteWakeFrame(room_id="客厅", detected_at=datetime.now(UTC))
    )
    transfers: list[tuple[UUID, UUID, UUID]] = []

    async def transfer(user_id: UUID, from_device_id: UUID, to_device_id: UUID) -> None:
        transfers.append((user_id, from_device_id, to_device_id))

    gateway.set_satellite_takeover_handler(transfer)

    async def handle(*args: Any) -> bool:
        emit = args[-1]
        await emit("reply.committed", {"content": "跟我去卧室"})
        return True

    gateway.set_satellite_utterance_handler(handle)
    await _submit_satellite_pcm(gateway, source)
    await gateway._satellite_tasks[source.principal.device_id]
    available = target_ws.frames[-1]
    assert available["type"] == "satellite.session.available"
    assert available["from_device_id"] == str(source.principal.device_id)
    await gateway.satellite_takeover(
        target, SatelliteTakeoverFrame(from_device_id=source.principal.device_id)
    )

    source_session = gateway.satellites.get(source.principal.device_id)
    target_session = gateway.satellites.get(target.principal.device_id)
    assert source_session is not None and source_session.state is SatelliteState.IDLE
    assert target_session is not None and target_session.state is SatelliteState.LISTENING
    assert source_ws.frames[-1]["type"] == "satellite.session.transferred"
    assert target_ws.frames[-1]["type"] == "satellite.session.accepted"
    assert transfers == [(owner, source.principal.device_id, target.principal.device_id)]
    await gateway.satellite_cancel(target, SatelliteCancelFrame(reason_code="test_done"))


async def test_follow_up_takeover_rejects_different_owner() -> None:
    gateway = _gateway()
    source_owner = uuid7()
    _, source = _connection(gateway, source_owner)
    target_ws, target = _connection(gateway, uuid7())
    await gateway.connect(source)
    await gateway.connect(target)
    from app.satellite import SatelliteHelloFrame, SatelliteTakeoverFrame

    await gateway.satellite_hello(source, SatelliteHelloFrame(room_id="客厅"))
    await gateway.satellite_hello(target, SatelliteHelloFrame(room_id="卧室"))
    source_session = gateway.satellites.get(source.principal.device_id)
    assert source_session is not None
    source_session.state = SatelliteState.LISTENING
    source_session.follow_up_until = datetime.now(UTC) + timedelta(seconds=30)

    await gateway.satellite_takeover(
        target, SatelliteTakeoverFrame(from_device_id=source.principal.device_id)
    )

    assert target_ws.frames[-1]["reason_code"] == "takeover_not_allowed"
    assert source_session.state is SatelliteState.LISTENING
