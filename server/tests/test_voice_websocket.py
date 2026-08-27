# ruff: noqa: RUF001, RUF003
"""语音通道端到端测试：fake ASR/TTS 走完整回路、打断仲裁与 L2 出站拒绝。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.api import create_voice_websocket_router
from app.api.voice_ws import VOICE_CONTEXT_MESSAGES, VoiceSession, VoiceWebSocketManager
from app.auth import AuthService, ChatPrincipal
from app.chat import ChatService, PendingTurn
from app.config import DatabaseConfigStore
from app.db import Base, Database, create_database
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult, ModelUsage
from app.schemas import PrivacyLevel
from app.tools import ClientLocation
from app.voice import SpeechRecognitionUnavailable, StaticVoiceSource, TtsProviderChain


def config_yaml() -> str:
    return """
schema_version: 1
models:
  cloud:
    provider: openai_compatible
    model: dialogue-v1
    base_url: https://models.example/v1
    secret_ref: env:MODEL_API_KEY
    runs_local: false
    max_privacy_level: L1
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
  local:
    provider: openai_compatible
    model: local-model
    base_url: http://127.0.0.1:11434/v1
    runs_local: true
    max_privacy_level: L2
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
routes:
  dialogue: {primary: cloud}
  utility: {primary: cloud}
  private: {primary: local}
"""


class StreamingBackend:
    def __init__(self, *, delta_delay_s: float = 0.0) -> None:
        self._delta_delay_s = delta_delay_s

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        return self._result(request, "hello world")

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        for delta in ("你好呀。今天", "天气不错！"):
            if self._delta_delay_s:
                await asyncio.sleep(self._delta_delay_s)
            await on_delta(delta)
        return self._result(request, "你好呀。今天天气不错！")

    def _result(self, request: CompletionRequest, text: str) -> CompletionResult:
        return CompletionResult(
            text=text,
            provider="openai_compatible",
            model="dialogue-v1",
            endpoint="cloud",
            route=request.route,
            usage=ModelUsage(input_tokens=3, output_tokens=5, total_tokens=8),
            latency_ms=1,
        )


class RemainderOnlyBackend(StreamingBackend):
    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        text = "最后一句没有标点"
        await on_delta(text)
        return self._result(request, text)


class FakeRecognizer:
    """云端识别器替身：恒定转写，可选延迟。"""

    def __init__(self, transcript: str, *, delay_s: float = 0.0) -> None:
        self.runs_local = False
        self._transcript = transcript
        self._delay_s = delay_s

    async def transcribe(
        self, pcm: bytes, *, sample_rate: int, language: str | None
    ) -> str:
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return self._transcript


class UnavailableLocalRecognizer:
    runs_local = True

    async def transcribe(
        self, pcm: bytes, *, sample_rate: int, language: str | None
    ) -> str:
        raise SpeechRecognitionUnavailable("faster_whisper_not_installed")


class SlowLocalRecognizer:
    runs_local = True

    async def transcribe(
        self, pcm: bytes, *, sample_rate: int, language: str | None
    ) -> str:
        await asyncio.sleep(10)
        return "不应该完成"


class FakeSynthesizer:
    """合成器替身：产出固定 PCM 分片，可选分片间隔制造打断窗口。"""

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.runs_local = False
        self.mime = "audio/pcm;rate=24000"
        self.sample_rate = 24_000
        self._delay_s = delay_s

    async def synthesize(
        self, text: str, *, privacy_level: PrivacyLevel
    ) -> AsyncIterator[bytes]:
        for chunk in (b"\x01\x00\x02\x00", b"\x03\x00\x04\x00"):
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            yield chunk


class FakeWakeWord:
    def __init__(self, *, detect_after: int | None) -> None:
        self.backend = "openwakeword"
        self._detect_after = detect_after
        self._calls = 0
        self.resets = 0

    def feed(self, pcm: bytes) -> bool:
        del pcm
        self._calls += 1
        detected = self._detect_after is not None and self._calls >= self._detect_after
        if detected:
            self.reset()
        return detected

    def reset(self) -> None:
        self.resets += 1


class RecordingWebSocket:
    def __init__(self) -> None:
        self.texts: list[dict[str, Any]] = []
        self.audio: list[bytes] = []
        self._closed = asyncio.Event()

    async def receive(self) -> dict[str, str]:
        await self._closed.wait()
        return {"type": "websocket.disconnect"}

    async def send_text(self, value: str) -> None:
        self.texts.append(json.loads(value))

    async def send_bytes(self, value: bytes) -> None:
        self.audio.append(value)

    def close(self) -> None:
        self._closed.set()


async def test_proactive_voice_reaches_matching_session_and_blocks_cloud_l2_tts() -> None:
    user_id = uuid7()
    socket = RecordingWebSocket()
    manager = VoiceWebSocketManager(
        cast(ChatService, object()),
        voice_source=StaticVoiceSource(None, TtsProviderChain([FakeSynthesizer()])),
    )
    session = VoiceSession(
        websocket=cast(WebSocket, socket),
        principal=ChatPrincipal(
            session_id=uuid7(),
            user_id=user_id,
            display_name="Owner",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
        conversation_id=uuid7(),
        privacy_level=PrivacyLevel.L2,
        wake_word=None,
    )
    run_task = asyncio.create_task(manager.run(session))
    await asyncio.sleep(0)

    delivered = await manager.broadcast_proactive(
        user_id,
        "回来啦。",
        privacy_level=PrivacyLevel.L2,
    )
    not_delivered = await manager.broadcast_proactive(
        uuid7(),
        "不应送达。",
        privacy_level=PrivacyLevel.L1,
    )
    socket.close()
    await run_task

    assert delivered == 1
    assert not_delivered == 0
    assert [event["type"] for event in socket.texts] == [
        "proactive.committed",
        "voice.tts_unavailable",
    ]
    assert socket.audio == []


def loud_frames(count: int) -> bytes:
    return b"\x40\x1f" * (480 * count)


def silent_frames(count: int) -> bytes:
    return b"\x00\x00" * (480 * count)


def _receive_until(
    websocket: Any, *stop_types: str
) -> tuple[list[dict[str, Any]], list[bytes]]:
    """阻塞收帧直到出现任一停止事件；返回 (JSON 事件, 音频分片)。"""
    events: list[dict[str, Any]] = []
    audio: list[bytes] = []
    while not events or events[-1].get("type") not in stop_types:
        message = websocket.receive()
        if "text" in message:
            events.append(json.loads(message["text"]))
        elif "bytes" in message:
            audio.append(message["bytes"])
    return events, audio


def _build(
    tmp_path: Path,
    *,
    delta_delay_s: float = 0.0,
    tts_delay_s: float = 0.0,
    backend: StreamingBackend | None = None,
    wake_word: FakeWakeWord | None = None,
    synthesizer: FakeSynthesizer | None = None,
    recognizer: Any | None = None,
    service_factory: (
        Callable[[Database, DatabaseConfigStore, StreamingBackend], ChatService] | None
    ) = None,
) -> tuple[FastAPI, str, str]:
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'voice.db'}")
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    store = DatabaseConfigStore(database, config_path)
    auth = AuthService(database)
    runtime_backend = backend or StreamingBackend(delta_delay_s=delta_delay_s)
    service = (
        service_factory(database, store, runtime_backend)
        if service_factory is not None
        else ChatService(
            database, store, router_builder=lambda config: runtime_backend
        )
    )

    async def setup() -> tuple[str, str]:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await store.load()
        session = await auth.setup(
            display_name="Owner", password="correct horse battery staple"
        )
        conversation = await service.create_conversation(
            user_id=session.principal.user_id, title="语音"
        )
        return session.access_token, str(conversation.id)

    token, conversation_id = asyncio.run(setup())
    app = FastAPI()
    router, _ = create_voice_websocket_router(
        service,
        auth,
        voice_source=StaticVoiceSource(
            recognizer or FakeRecognizer("帮我看看今天适合穿什么"),
            TtsProviderChain(
                [synthesizer or FakeSynthesizer(delay_s=tts_delay_s)]
            ),
        ),
        wake_word_factory=lambda: wake_word,
    )
    app.include_router(router)
    return app, token, conversation_id


def _build_with_recognizer(
    tmp_path: Path, recognizer: Any
) -> tuple[FastAPI, str, str]:
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'voice-unavailable.db'}")
    config_path = tmp_path / "hub-unavailable.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    store = DatabaseConfigStore(database, config_path)
    auth = AuthService(database)
    service = ChatService(database, store, router_builder=lambda config: StreamingBackend())

    async def setup() -> tuple[str, str]:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await store.load()
        session = await auth.setup(
            display_name="Owner", password="correct horse battery staple"
        )
        conversation = await service.create_conversation(
            user_id=session.principal.user_id, title="语音"
        )
        return session.access_token, str(conversation.id)

    token, conversation_id = asyncio.run(setup())
    app = FastAPI()
    router, _ = create_voice_websocket_router(
        service,
        auth,
        voice_source=StaticVoiceSource(recognizer, None),
    )
    app.include_router(router)
    return app, token, conversation_id


def test_voice_websocket_full_loop_with_sentences_and_audio(tmp_path: Path) -> None:
    app, token, conversation_id = _build(tmp_path)

    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as invalid,
            client.websocket_connect("/ws/voice") as websocket,
        ):
            websocket.send_json({"type": "authenticate", "access_token": "nope"})
            websocket.receive_json()
        assert invalid.value.code == 4401

        with client.websocket_connect("/ws/voice") as websocket:
            websocket.send_json({"type": "authenticate", "access_token": token})
            websocket.send_json(
                {
                    "type": "voice.hello",
                    "conversation_id": conversation_id,
                    "privacy_level": "L1",
                    "format": "pcm_s16le",
                    "sample_rate": 16000,
                    "channels": 1,
                }
            )
            events, audio = _receive_until(websocket, "voice.ready")
            assert events[-1]["type"] == "voice.ready"
            assert events[-1]["asr_configured"] is True
            assert events[-1]["asr_runs_local"] is False
            assert events[-1]["tts_configured"] is True

            # VAD 断句：响帧触发开始，静音 hangover 触发结束
            websocket.send_bytes(loud_frames(12))
            websocket.send_bytes(silent_frames(25))

            events, audio = _receive_until(websocket, "reply.committed")

    types = [event["type"] for event in events]
    assert "voice.transcript" in types
    assert "turn.accepted" in types
    assert [event["delta"] for event in events if event["type"] == "reply.delta"] == [
        "你好呀。今天",
        "天气不错！",
    ]
    assert types.count("voice.sentence") == 2
    assert types.count("voice.sentence.end") == 2
    assert types[-1] == "reply.committed"
    transcript = next(event for event in events if event["type"] == "voice.transcript")
    assert transcript["text"] == "帮我看看今天适合穿什么"
    sentence = next(event for event in events if event["type"] == "voice.sentence")
    assert sentence["mime"] == "audio/pcm;rate=24000"
    assert sentence["sample_rate"] == 24000
    assert sentence["provider"] == "FakeSynthesizer"
    # 两句话 × 每句两个分片
    assert len(audio) == 4

    with TestClient(app) as client:
        report = client.get("/api/v1/meta/voice/latency")
    assert report.status_code == 200
    body = report.json()
    assert body["count"] == 1
    assert body["total_ms"]["count"] == 1

    with TestClient(app) as client:
        unauthorized = client.post("/api/v1/meta/voice/latency/reset")
        reset = client.post(
            "/api/v1/meta/voice/latency/reset",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert unauthorized.status_code == 401
    assert reset.status_code == 200
    assert reset.json()["count"] == 0


def test_voice_websocket_accepts_text_and_replies_with_audio(tmp_path: Path) -> None:
    app, token, conversation_id = _build(
        tmp_path,
        # 直接文字回合不得调用 ASR；这个识别器若被调用会立即失败。
        recognizer=UnavailableLocalRecognizer(),
    )

    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": "L1",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        _receive_until(websocket, "voice.ready")
        websocket.send_json({"type": "text.submit", "text": "这是文字输入"})
        events, audio = _receive_until(websocket, "reply.committed")

    types = [event["type"] for event in events]
    assert types[0] == "voice.transcript"
    assert events[0]["text"] == "这是文字输入"
    assert "turn.accepted" in types
    assert types.count("reply.delta") == 2
    assert types[-1] == "reply.committed"
    assert len(audio) == 4


def test_voice_websocket_limits_turn_context_window(tmp_path: Path) -> None:
    class ContextSpyChatService(ChatService):
        seen_context_windows: ClassVar[list[int | None]] = []

        async def start_turn(
            self,
            conversation_id: UUID,
            *,
            user_id: UUID,
            text: str,
            privacy_level: PrivacyLevel,
            max_context_messages: int | None = None,
            client_location: ClientLocation | None = None,
        ) -> PendingTurn:
            self.seen_context_windows.append(max_context_messages)
            return await super().start_turn(
                conversation_id,
                user_id=user_id,
                text=text,
                privacy_level=privacy_level,
                max_context_messages=max_context_messages,
                client_location=client_location,
            )

    def spy_factory(
        database: Database, store: DatabaseConfigStore, backend: StreamingBackend
    ) -> ChatService:
        return ContextSpyChatService(
            database, store, router_builder=lambda config: backend
        )

    app, token, conversation_id = _build(tmp_path, service_factory=spy_factory)

    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": "L1",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        _receive_until(websocket, "voice.ready")
        websocket.send_json({"type": "text.submit", "text": "语音上下文裁剪"})
        _receive_until(websocket, "reply.committed")

    assert ContextSpyChatService.seen_context_windows == [VOICE_CONTEXT_MESSAGES]


def test_voice_websocket_emits_viseme_for_pcm_tts(tmp_path: Path) -> None:
    class VisemeSynthesizer(FakeSynthesizer):
        async def synthesize(
            self, text: str, *, privacy_level: PrivacyLevel
        ) -> AsyncIterator[bytes]:
            del text, privacy_level
            # 50ms at 24kHz PCM16 = 2400 bytes
            yield b"\x40\x1f" * 1_200

    app, token, conversation_id = _build(
        tmp_path,
        synthesizer=VisemeSynthesizer(),
    )
    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": "L1",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        _receive_until(websocket, "voice.ready")
        websocket.send_json({"type": "utterance.begin"})
        websocket.send_bytes(loud_frames(10))
        websocket.send_json({"type": "utterance.end"})
        events, _ = _receive_until(websocket, "reply.committed")

    visemes = [event for event in events if event["type"] == "voice.viseme"]
    assert len(visemes) == 2
    assert visemes[0]["duration_ms"] == 50
    assert visemes[0]["offset_ms"] == 0
    assert visemes[0]["amp"] == pytest.approx(round(8000 / 32768, 4))


def test_voice_websocket_wake_word_gates_automatic_listening(tmp_path: Path) -> None:
    wake_word = FakeWakeWord(detect_after=2)
    app, token, conversation_id = _build(tmp_path, wake_word=wake_word)

    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": "L1",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        ready, _ = _receive_until(websocket, "voice.ready")
        assert ready[-1]["wake_word_configured"] is True
        assert ready[-1]["wake_word_backend"] == "openwakeword"

        # 第一块音频只喂唤醒词，不进入 VAD / ASR。
        websocket.send_bytes(loud_frames(4))
        # 第二块命中唤醒词，仅解除待命门并回事件。
        websocket.send_bytes(loud_frames(4))
        events, _ = _receive_until(websocket, "voice.wake_detected")
        assert events[-1]["backend"] == "openwakeword"

        # 唤醒后才允许 VAD 完成一次话语。
        websocket.send_bytes(loud_frames(12))
        websocket.send_bytes(silent_frames(25))
        events, _ = _receive_until(websocket, "reply.committed")

    assert "voice.transcript" in [event["type"] for event in events]
    assert wake_word.resets >= 2


def test_voice_websocket_ptt_bypasses_wake_word_gate(tmp_path: Path) -> None:
    wake_word = FakeWakeWord(detect_after=None)
    app, token, conversation_id = _build(tmp_path, wake_word=wake_word)

    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": "L1",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        _receive_until(websocket, "voice.ready")
        websocket.send_json({"type": "utterance.begin"})
        websocket.send_bytes(loud_frames(10))
        websocket.send_json({"type": "utterance.end"})
        events, _ = _receive_until(websocket, "reply.committed")

    assert "voice.wake_detected" not in [event["type"] for event in events]
    assert wake_word.resets >= 1


def test_voice_websocket_barge_in_cancels_turn_and_tts(tmp_path: Path) -> None:
    # TTS 分片间隔拉开打断窗口：首句播报中途注入人声
    app, token, conversation_id = _build(tmp_path, tts_delay_s=0.2)

    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": "L1",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        _receive_until(websocket, "voice.ready")
        websocket.send_bytes(loud_frames(12))
        websocket.send_bytes(silent_frames(25))
        events, _ = _receive_until(websocket, "voice.sentence")
        assert events[-1]["type"] == "voice.sentence"

        # 回合进行中的人声能量 → 立即打断
        websocket.send_bytes(loud_frames(4))
        events, _ = _receive_until(websocket, "turn.cancelled")
        report = client.get("/api/v1/meta/voice/latency")

    types = [event["type"] for event in events]
    assert "voice.interrupted" in types
    assert types[-1] == "turn.cancelled"
    assert "reply.committed" not in types
    interrupted = next(event for event in events if event["type"] == "voice.interrupted")
    assert interrupted["reason"] == "barge_in"
    assert report.status_code == 200
    body = report.json()
    assert body["interrupt_ms"]["count"] == 1
    assert body["targets"]["interrupt_p90_ms"] == 300


def test_voice_websocket_m2_soak_20_complete_and_20_interrupts(tmp_path: Path) -> None:
    app, token, conversation_id = _build(tmp_path, tts_delay_s=0.01)

    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": "L1",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        _receive_until(websocket, "voice.ready")

        for _ in range(20):
            websocket.send_json({"type": "utterance.begin"})
            websocket.send_bytes(loud_frames(10))
            websocket.send_json({"type": "utterance.end"})
            events, _ = _receive_until(websocket, "reply.committed")
            assert events[-1]["type"] == "reply.committed"

        for _ in range(20):
            websocket.send_json({"type": "utterance.begin"})
            websocket.send_bytes(loud_frames(10))
            websocket.send_json({"type": "utterance.end"})
            events, _ = _receive_until(websocket, "voice.sentence")
            assert events[-1]["type"] == "voice.sentence"
            websocket.send_json({"type": "interrupt"})
            events, _ = _receive_until(websocket, "turn.cancelled")
            assert "voice.interrupted" in [event["type"] for event in events]

        report = client.get("/api/v1/meta/voice/latency")

    assert report.status_code == 200
    body = report.json()
    assert body["count"] == 20
    assert body["interrupt_ms"]["count"] == 20
    assert body["acceptance"] == {
        "completed_turns_ready": True,
        "interrupt_samples_ready": True,
        "first_audio_p90_pass": True,
        "interrupt_p90_pass": True,
    }


def test_voice_websocket_rejects_l2_audio_for_cloud_asr(tmp_path: Path) -> None:
    app, token, conversation_id = _build(tmp_path)

    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": "L2",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        _receive_until(websocket, "voice.ready")
        websocket.send_bytes(loud_frames(12))
        websocket.send_bytes(silent_frames(25))

        events, _ = _receive_until(websocket, "voice.asr_unavailable")

    assert events[-1]["reason"] == "local_asr_required"
    assert all(event["type"] != "turn.accepted" for event in events)


def test_voice_ready_reports_unconfigured_asr_before_recording(tmp_path: Path) -> None:
    app, token, conversation_id = _build_with_recognizer(tmp_path, None)

    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": "L1",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        events, _ = _receive_until(websocket, "voice.ready")

    assert events[-1]["asr_configured"] is False
    assert events[-1]["asr_runs_local"] is None
    assert events[-1]["tts_configured"] is False
    assert events[-1]["vad_backend"] in {"energy", "silero"}


def test_voice_websocket_ptt_mode_finalizes_on_explicit_end(tmp_path: Path) -> None:
    app, token, conversation_id = _build(tmp_path)

    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": "L1",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        _receive_until(websocket, "voice.ready")
        # PTT：显式边界优先，静音帧不参与断句
        websocket.send_json({"type": "utterance.begin"})
        websocket.send_bytes(loud_frames(10))
        websocket.send_bytes(silent_frames(5))
        websocket.send_json({"type": "utterance.end"})

        events, audio = _receive_until(websocket, "reply.committed")

    assert [event["type"] for event in events][-1] == "reply.committed"
    assert len(audio) == 4


def test_voice_websocket_reports_local_asr_dependency_unavailable(tmp_path: Path) -> None:
    app, token, conversation_id = _build_with_recognizer(
        tmp_path, UnavailableLocalRecognizer()
    )

    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": "L2",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        _receive_until(websocket, "voice.ready")
        websocket.send_json({"type": "utterance.begin"})
        websocket.send_bytes(loud_frames(10))
        websocket.send_json({"type": "utterance.end"})
        events, _ = _receive_until(websocket, "voice.asr_unavailable")

    assert events[-1]["reason"] == "faster_whisper_not_installed"
    assert all(event["type"] != "turn.accepted" for event in events)


def test_voice_websocket_interrupts_during_asr_before_generation(tmp_path: Path) -> None:
    app, token, conversation_id = _build_with_recognizer(tmp_path, SlowLocalRecognizer())

    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": "L2",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        _receive_until(websocket, "voice.ready")
        websocket.send_json({"type": "utterance.begin"})
        websocket.send_bytes(loud_frames(10))
        websocket.send_json({"type": "utterance.end"})
        websocket.send_json({"type": "interrupt"})
        events, _ = _receive_until(websocket, "voice.interrupted")

    assert events[-1]["generation_id"] is None
    assert events[-1]["reason"] == "client_interrupt"
    assert all(event["type"] != "turn.accepted" for event in events)


def test_voice_interrupt_stops_post_commit_remainder_tts_without_cancelling_turn(
    tmp_path: Path,
) -> None:
    app, token, conversation_id = _build(
        tmp_path,
        backend=RemainderOnlyBackend(),
        tts_delay_s=0.2,
    )

    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": "L1",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        _receive_until(websocket, "voice.ready")
        websocket.send_json({"type": "utterance.begin"})
        websocket.send_bytes(loud_frames(10))
        websocket.send_json({"type": "utterance.end"})
        _receive_until(websocket, "voice.sentence")
        websocket.send_json({"type": "interrupt"})
        events, _ = _receive_until(websocket, "voice.interrupted")

    assert events[-1]["turn_cancelled"] is False
    assert events[-1]["reason"] == "client_interrupt"
