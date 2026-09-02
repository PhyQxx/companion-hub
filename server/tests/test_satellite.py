from __future__ import annotations

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
    SatelliteEvent,
    SatelliteRegistry,
    SatelliteState,
    next_state,
)

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
