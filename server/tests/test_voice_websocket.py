# ruff: noqa: RUF001, RUF002, RUF003
"""语音通道端到端测试：fake ASR/TTS 走完整回路、打断仲裁与 L2 出站拒绝。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import create_voice_websocket_router
from app.auth import AuthService
from app.chat import ChatService
from app.config import DatabaseConfigStore
from app.db import Base, create_database
from app.llm import CompletionRequest, CompletionResult, ModelUsage
from app.schemas import PrivacyLevel
from app.voice import TtsProviderChain


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
    tmp_path: Path, *, delta_delay_s: float = 0.0, tts_delay_s: float = 0.0
) -> tuple[FastAPI, str, str]:
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'voice.db'}")
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    store = DatabaseConfigStore(database, config_path)
    auth = AuthService(database)
    service = ChatService(
        database, store, router_builder=lambda config: StreamingBackend(
            delta_delay_s=delta_delay_s
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
        recognizer=FakeRecognizer("帮我看看今天适合穿什么"),
        tts_chain=TtsProviderChain([FakeSynthesizer(delay_s=tts_delay_s)]),
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

            # VAD 断句：响帧触发开始，静音 hangover 触发结束
            websocket.send_bytes(loud_frames(12))
            websocket.send_bytes(silent_frames(25))

            events, audio = _receive_until(websocket, "reply.committed")

    types = [event["type"] for event in events]
    assert "voice.transcript" in types
    assert "turn.accepted" in types
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

    types = [event["type"] for event in events]
    assert "voice.interrupted" in types
    assert types[-1] == "turn.cancelled"
    assert "reply.committed" not in types
    interrupted = next(event for event in events if event["type"] == "voice.interrupted")
    assert interrupted["reason"] == "barge_in"


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
