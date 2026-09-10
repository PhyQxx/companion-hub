"""SAT-01 房间语音卫星：状态机、唤醒仲裁与协议帧契约。

状态机（idle → listening → processing → speaking → idle）：
- wake_accepted 由 Hub 仲裁胜出后下发（设备不得自行进入 listening）；
- utterance_end / reply_ready / reply_done 由设备按 VAD/播放进度上报；
- cancel / error 任意状态可回到 idle。

SAT-02 就近响应核心：同一 owner 任一时刻只有一个卫星处于唤醒会话中；
仲裁窗口内的后续唤醒一律压制，保证多终端听到唤醒词时只有一个响应。
音频用带序号和摘要的签名 JSON 帧传输，保持设备通道只有一种鉴权边界。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
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
    BROADCAST_READY = "broadcast_ready"
    FOLLOW_UP_READY = "follow_up_ready"
    INTERRUPT = "interrupt"
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
    (SatelliteState.IDLE, SatelliteEvent.BROADCAST_READY): SatelliteState.SPEAKING,
    (SatelliteState.LISTENING, SatelliteEvent.UTTERANCE_END): SatelliteState.PROCESSING,
    (SatelliteState.LISTENING, SatelliteEvent.CANCEL): SatelliteState.IDLE,
    (SatelliteState.LISTENING, SatelliteEvent.ERROR): SatelliteState.IDLE,
    (SatelliteState.PROCESSING, SatelliteEvent.REPLY_READY): SatelliteState.SPEAKING,
    (SatelliteState.PROCESSING, SatelliteEvent.CANCEL): SatelliteState.IDLE,
    (SatelliteState.PROCESSING, SatelliteEvent.ERROR): SatelliteState.IDLE,
    (SatelliteState.PROCESSING, SatelliteEvent.REPLY_DONE): SatelliteState.IDLE,
    (SatelliteState.PROCESSING, SatelliteEvent.FOLLOW_UP_READY): SatelliteState.LISTENING,
    (SatelliteState.SPEAKING, SatelliteEvent.REPLY_DONE): SatelliteState.IDLE,
    (SatelliteState.SPEAKING, SatelliteEvent.FOLLOW_UP_READY): SatelliteState.LISTENING,
    (SatelliteState.PROCESSING, SatelliteEvent.INTERRUPT): SatelliteState.LISTENING,
    (SatelliteState.SPEAKING, SatelliteEvent.INTERRUPT): SatelliteState.LISTENING,
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
    max_privacy_level: Literal["L0", "L1", "L2"] = "L1"
    continuous_timeout_seconds: Annotated[float, Field(ge=0, le=30)] = 8


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


class SatelliteAudioStartFrame(StrictModel):
    proto_version: Literal[1] = 1
    type: Literal["satellite.audio.start"] = "satellite.audio.start"
    utterance_id: UUID
    format: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate: Literal[16000] = 16000
    channels: Literal[1] = 1
    privacy_level: Literal["L0", "L1", "L2"] = "L1"
    barge_in: bool = False


class SatelliteAudioChunkFrame(StrictModel):
    proto_version: Literal[1] = 1
    type: Literal["satellite.audio.chunk"] = "satellite.audio.chunk"
    utterance_id: UUID
    index: Annotated[int, Field(ge=0, le=4095)]
    data_b64: Annotated[str, Field(min_length=4, max_length=90_000)]


class SatelliteAudioEndFrame(StrictModel):
    proto_version: Literal[1] = 1
    type: Literal["satellite.audio.end"] = "satellite.audio.end"
    utterance_id: UUID
    chunks: Annotated[int, Field(ge=1, le=4096)]
    bytes: Annotated[int, Field(ge=1, le=4 * 1024 * 1024)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SatelliteCancelFrame(StrictModel):
    proto_version: Literal[1] = 1
    type: Literal["satellite.cancel"] = "satellite.cancel"
    utterance_id: UUID | None = None
    reason_code: Annotated[str, Field(min_length=1, max_length=64)] = "device_cancelled"


class SatelliteTakeoverFrame(StrictModel):
    proto_version: Literal[1] = 1
    type: Literal["satellite.takeover"] = "satellite.takeover"
    from_device_id: UUID


class SatelliteSessionView(StrictModel):
    device_id: UUID
    owner_user_id: UUID
    room_id: str
    max_privacy_level: Literal["L0", "L1", "L2"]
    continuous_timeout_seconds: float
    state: SatelliteState
    wake_count: int
    suppressed_count: int
    last_seen_at: datetime | None = None
    follow_up_until: datetime | None = None
