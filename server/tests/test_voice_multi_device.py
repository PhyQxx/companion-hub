"""多端音频与麦克风租约：抢占通知、采集互斥与桌宠播报纳管。

策略：同类型租约"最新获取者抢占，旧持有者尽快停止"。
- 麦克风：话语采集开始时获取，被抢占方收到 voice.microphone_preempted 并停止采集；
- 音频输出：新语音回合获取时抢占旧持有者，打断其进行中的回合；
- 桌宠播报：持有 audio_output，逐块检查持有权，被抢占即中止剩余音频。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import WebSocket
from test_runtime import FakeChatService

from app.api.voice_ws import VoiceSession, VoiceWebSocketManager
from app.auth import ChatPrincipal
from app.chat import ChatService
from app.db import Base, Database, create_database
from app.runtime import TurnCoordinator
from app.schemas import PrivacyLevel
from app.voice import StaticVoiceSource, TtsProviderChain


class FakeSynth:
    """合成器替身：先出一块，等 gate 放行再出第二块（制造抢占窗口）。"""

    runs_local = False
    mime = "audio/pcm;rate=24000"
    sample_rate = 24_000

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    async def synthesize(
        self, text: str, *, privacy_level: PrivacyLevel
    ) -> AsyncIterator[bytes]:
        del text, privacy_level
        yield b"\x01\x00\x02\x00"
        await self.gate.wait()
        yield b"\x03\x00\x04\x00"


class RecordingSocket:
    def __init__(self) -> None:
        self.texts: list[dict[str, Any]] = []
        self.closed = asyncio.Event()

    async def receive(self) -> dict[str, str]:
        await self.closed.wait()
        return {"type": "websocket.disconnect"}

    async def send_text(self, value: str) -> None:
        self.texts.append(json.loads(value))

    async def send_bytes(self, value: bytes) -> None:
        del value

    def close(self) -> None:
        self.closed.set()


def _session(user_id: UUID, socket: RecordingSocket) -> VoiceSession:
    return VoiceSession(
        websocket=cast(WebSocket, socket),
        principal=ChatPrincipal(
            session_id=uuid4(),
            user_id=user_id,
            display_name="Owner",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
        wake_word=None,
    )


@pytest.fixture
async def database() -> Any:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield value
    await value.close()


@pytest.fixture
async def coordinator(database: Database) -> TurnCoordinator:
    return TurnCoordinator(database, FakeChatService())


@pytest.fixture
async def manager(coordinator: TurnCoordinator) -> VoiceWebSocketManager:
    return VoiceWebSocketManager(
        cast(ChatService, object()),
        voice_source=StaticVoiceSource(None, TtsProviderChain([FakeSynth()])),
        turn_coordinator=coordinator,
    )


async def _register(
    manager: VoiceWebSocketManager, session: VoiceSession, socket: RecordingSocket
) -> asyncio.Task[None]:
    task = asyncio.create_task(manager.run(session))
    await asyncio.sleep(0)
    return task


def _events(socket: RecordingSocket, event_type: str) -> list[dict[str, Any]]:
    return [frame for frame in socket.texts if frame.get("type") == event_type]


async def test_microphone_lease_preempts_other_collector(
    manager: VoiceWebSocketManager, coordinator: TurnCoordinator
) -> None:
    socket_a, socket_b = RecordingSocket(), RecordingSocket()
    session_a = _session(uuid4(), socket_a)
    session_b = _session(uuid4(), socket_b)
    run_a = await _register(manager, session_a, socket_a)
    run_b = await _register(manager, session_b, socket_b)
    try:
        await manager._on_control(session_a, json.dumps({"type": "utterance.begin"}))
        assert session_a.collecting is True

        await manager._on_control(session_b, json.dumps({"type": "utterance.begin"}))

        assert session_b.collecting is True
        assert session_a.collecting is False
        assert session_a.ptt_active is False
        assert len(session_a.utterance) == 0
        preempted = _events(socket_a, "voice.microphone_preempted")
        assert len(preempted) == 1
        assert preempted[0]["by_device"] == str(session_b.device_id)

        await manager._on_control(session_b, json.dumps({"type": "utterance.end"}))
        # B 结束采集后麦克风租约释放：第三方获取不再看到旧持有者
        third = await coordinator.acquire_microphone(uuid4())
        assert third.previous_holder is None
    finally:
        socket_a.close()
        socket_b.close()
        run_a.cancel()
        run_b.cancel()


async def test_audio_lease_preemption_notifies_and_interrupts_holder(
    manager: VoiceWebSocketManager, coordinator: TurnCoordinator
) -> None:
    socket_a, socket_b = RecordingSocket(), RecordingSocket()
    session_a = _session(uuid4(), socket_a)
    session_b = _session(uuid4(), socket_b)
    run_a = await _register(manager, session_a, socket_a)
    run_b = await _register(manager, session_b, socket_b)
    holder_task: asyncio.Task[None] | None = None
    try:
        await coordinator.acquire_audio_lease(session_a.device_id, uuid4())
        holder_task = asyncio.create_task(asyncio.sleep(60))
        session_a.turn_task = holder_task

        lease = await coordinator.acquire_audio_lease(session_b.device_id, uuid4())
        assert lease.previous_holder == session_a.device_id
        await manager._preempt_audio_holder(session_a.device_id, exclude=session_b)

        assert _events(socket_a, "voice.audio_preempted")
        interrupted = _events(socket_a, "voice.interrupted")
        assert interrupted[0]["reason"] == "audio_lease_preempted"
        with pytest.raises(asyncio.CancelledError):
            await holder_task
        assert await coordinator.current_audio_holder() == session_b.device_id
    finally:
        if holder_task is not None and not holder_task.done():
            holder_task.cancel()
        socket_a.close()
        socket_b.close()
        run_a.cancel()
        run_b.cancel()


async def test_pet_speech_aborts_when_audio_preempted(
    coordinator: TurnCoordinator,
) -> None:
    synth = FakeSynth()
    manager = VoiceWebSocketManager(
        cast(ChatService, object()),
        voice_source=StaticVoiceSource(None, TtsProviderChain([synth])),
        turn_coordinator=coordinator,
    )
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(frame_type: str, payload: dict[str, Any]) -> None:
        emitted.append((frame_type, payload))

    speech = asyncio.create_task(
        manager.stream_device_speech("你好呀", PrivacyLevel.L1, emit)
    )
    # 等第一块音频发出（此时桌宠持有租约）
    for _ in range(100):
        if any(frame == "pet.audio.chunk" for frame, _ in emitted):
            break
        await asyncio.sleep(0.01)
    assert any(frame == "pet.audio.chunk" for frame, _ in emitted)

    other = uuid4()
    await coordinator.acquire_audio_lease(other, uuid4())

    # 放行第二块：下一轮持有权检查应失败并中止
    synth.gate.set()
    assert await speech is False

    frames = [frame for frame, _ in emitted]
    assert frames[-1] == "pet.audio.failed"
    assert emitted[-1][1] == {"reason_code": "audio_preempted"}
    assert "pet.audio.end" not in frames
    # 桌宠中止不释放他人租约
    assert await coordinator.current_audio_holder() == other


async def test_release_for_generation(
    coordinator: TurnCoordinator,
) -> None:
    generation = uuid4()
    device = uuid4()
    await coordinator.acquire_audio_lease(device, generation)
    assert await coordinator.current_audio_holder() == device

    assert await coordinator.release_audio_lease_for_generation(generation) is True
    assert await coordinator.current_audio_holder() is None
    assert await coordinator.release_audio_lease_for_generation(generation) is False

