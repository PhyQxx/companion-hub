"""SAT-01 房间语音卫星：状态机、唤醒仲裁与协议帧契约。

状态机（idle → listening → processing → speaking → idle）：
- wake_accepted 由 Hub 仲裁胜出后下发（设备不得自行进入 listening）；
- utterance_end / reply_ready / reply_done 由设备按 VAD/播放进度上报；
- cancel / error 任意状态可回到 idle。

SAT-02 就近响应核心：同一 owner 任一时刻只有一个卫星处于唤醒会话中；
仲裁窗口内的后续唤醒一律压制，保证多终端听到唤醒词时只有一个响应。
音频上下行帧在真机（旧手机/树莓派）验证协议时再落地，先不实现。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import Field

from app.schemas.common import StrictModel

SATELLITE_CAPABILITY = "voice.satellite"


class SatelliteState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"


class SatelliteEvent(StrEnum):
    WAKE_ACCEPTED = "wake_accepted"
    UTTERANCE_END = "utterance_end"
    REPLY_READY = "reply_ready"
    REPLY_DONE = "reply_done"
    CANCEL = "cancel"
    ERROR = "error"


class InvalidSatelliteTransition(ValueError):
    def __init__(self, current: SatelliteState, event: SatelliteEvent) -> None:
        self.current = current
        self.event = event
        super().__init__(f"illegal satellite transition: {current.value} + {event.value}")


# (当前状态, 事件) → 目标状态；CANCEL/ERROR 任意状态回到 IDLE 单独处理
_TRANSITIONS: dict[tuple[SatelliteState, SatelliteEvent], SatelliteState] = {
    (SatelliteState.IDLE, SatelliteEvent.WAKE_ACCEPTED): SatelliteState.LISTENING,
    (SatelliteState.LISTENING, SatelliteEvent.UTTERANCE_END): SatelliteState.PROCESSING,
    (SatelliteState.LISTENING, SatelliteEvent.CANCEL): SatelliteState.IDLE,
    (SatelliteState.LISTENING, SatelliteEvent.ERROR): SatelliteState.IDLE,
    (SatelliteState.PROCESSING, SatelliteEvent.REPLY_READY): SatelliteState.SPEAKING,
    (SatelliteState.PROCESSING, SatelliteEvent.CANCEL): SatelliteState.IDLE,
    (SatelliteState.PROCESSING, SatelliteEvent.ERROR): SatelliteState.IDLE,
    (SatelliteState.PROCESSING, SatelliteEvent.REPLY_DONE): SatelliteState.IDLE,
    (SatelliteState.SPEAKING, SatelliteEvent.REPLY_DONE): SatelliteState.IDLE,
    (SatelliteState.SPEAKING, SatelliteEvent.CANCEL): SatelliteState.IDLE,
    (SatelliteState.SPEAKING, SatelliteEvent.ERROR): SatelliteState.IDLE,
}

# 设备可自行上报的目标状态 → 等价事件（idle→listening 只能由 Hub 仲裁触发）
_DEVICE_REPORTED_EVENTS: dict[SatelliteState, SatelliteEvent] = {
    SatelliteState.PROCESSING: SatelliteEvent.UTTERANCE_END,
    SatelliteState.SPEAKING: SatelliteEvent.REPLY_READY,
    SatelliteState.IDLE: SatelliteEvent.REPLY_DONE,
}


def next_state(current: SatelliteState, event: SatelliteEvent) -> SatelliteState:
    if event in {SatelliteEvent.CANCEL, SatelliteEvent.ERROR}:
        return SatelliteState.IDLE
    target = _TRANSITIONS.get((current, event))
    if target is None:
        raise InvalidSatelliteTransition(current, event)
    return target


def device_reported_event(target: SatelliteState) -> SatelliteEvent | None:
    """设备上报的目标状态映射为事件；返回 None 表示该转移不归设备驱动。"""
    return _DEVICE_REPORTED_EVENTS.get(target)


class SatelliteHelloFrame(StrictModel):
    proto_version: int = 1
    type: str = "satellite.hello"
    room_id: Annotated[str, Field(min_length=1, max_length=64)]
    firmware: Annotated[str, Field(min_length=1, max_length=80)] | None = None


class SatelliteWakeFrame(StrictModel):
    proto_version: int = 1
    type: str = "satellite.wake"
    room_id: Annotated[str, Field(min_length=1, max_length=64)]
    detected_at: datetime
    confidence: Annotated[float, Field(ge=0, le=1)] = 1.0


class SatelliteStateFrame(StrictModel):
    proto_version: int = 1
    type: str = "satellite.state"
    state: SatelliteState
    reason_code: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class SatelliteSessionView(StrictModel):
    device_id: UUID
    owner_user_id: UUID
    room_id: str
    state: SatelliteState
    wake_count: int
    suppressed_count: int
    last_seen_at: datetime | None = None
