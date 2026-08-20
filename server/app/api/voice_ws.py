# ruff: noqa: RUF002, RUF003
"""P6 语音通道（docs/33 §3.1）：/ws/voice 双向音频流。

上行：PCM16 音频二进制帧 + JSON 控制（hello / PTT 边界 / 打断）；
下行：转写、句级 TTS 音频分片（mime 按实际提供方声明）、回合生命周期
与打断事件。回合本体复用 ChatService（记忆、Timeline、隐私路由、取消
全部生效），语音层只做断句、转写、合成与打断仲裁。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.auth import AuthService, ChatPrincipal, InvalidSession
from app.chat import ChatService, PendingTurn, TurnCancelled
from app.llm import LLMRouteExhausted
from app.privacy import EgressBlocked
from app.schemas import PrivacyLevel
from app.voice import (
    EnergyVad,
    SentenceBuffer,
    SpeechRecognizer,
    TtsProviderChain,
    VoiceProviderSource,
)
from app.voice.contracts import LocalOnlySynthesizerError

from .chat_ws import AuthenticateFrame

logger = logging.getLogger(__name__)

SUPPORTED_FORMAT = "pcm_s16le"
SUPPORTED_SAMPLE_RATE = 16_000
SUPPORTED_CHANNELS = 1
MIN_UTTERANCE_BYTES = 4_800  # 150ms：短于该长度视为噪声丢弃


@dataclass(slots=True)
class VoiceSession:
    """一条语音连接的会话状态。"""

    websocket: WebSocket
    principal: ChatPrincipal
    conversation_id: UUID | None = None
    privacy_level: PrivacyLevel = PrivacyLevel.L1
    vad: EnergyVad = field(default_factory=EnergyVad)
    ptt_active: bool = False
    collecting: bool = False
    utterance: bytearray = field(default_factory=bytearray)
    generation_id: UUID | None = None
    turn_task: asyncio.Task[None] | None = None
    tts_unavailable_notified: bool = False


class VoiceWebSocketManager:
    def __init__(
        self,
        service: ChatService,
        *,
        voice_source: VoiceProviderSource,
    ) -> None:
        self._service = service
        self._voice_source = voice_source

    async def run(self, session: VoiceSession) -> None:
        """连接主循环：JSON 控制 + 二进制音频。"""
        websocket = session.websocket
        while True:
            message = await websocket.receive()
            kind = message.get("type")
            if kind == "websocket.disconnect":
                break
            if "text" in message:
                await self._on_control(session, message["text"])
            elif "bytes" in message:
                await self._on_audio(session, message["bytes"])

    async def _on_control(self, session: VoiceSession, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(frame, dict):
            return
        match frame.get("type"):
            case "voice.hello":
                await self._on_hello(session, frame)
            case "utterance.begin":
                session.ptt_active = True
                session.collecting = True
                session.utterance.clear()
            case "utterance.end":
                session.ptt_active = False
                await self._finalize_utterance(session)
            case "interrupt":
                await self._interrupt(session, reason="client_interrupt")

    async def _on_hello(self, session: VoiceSession, frame: dict[str, Any]) -> None:
        if (
            frame.get("format") != SUPPORTED_FORMAT
            or frame.get("sample_rate") != SUPPORTED_SAMPLE_RATE
            or frame.get("channels") != SUPPORTED_CHANNELS
        ):
            await self._send(
                session,
                "voice.format_unsupported",
                {
                    "supported": {
                        "format": SUPPORTED_FORMAT,
                        "sample_rate": SUPPORTED_SAMPLE_RATE,
                        "channels": SUPPORTED_CHANNELS,
                    }
                },
            )
            await session.websocket.close(code=4400, reason="unsupported audio format")
            return
        conversation_raw = frame.get("conversation_id")
        if not isinstance(conversation_raw, str):
            await self._send(session, "voice.error", {"reason": "conversation_id_required"})
            return
        session.conversation_id = UUID(conversation_raw)
        session.privacy_level = PrivacyLevel(frame.get("privacy_level", "L1"))
        await self._send(
            session,
            "voice.ready",
            {"conversation_id": str(session.conversation_id)},
        )

    async def _on_audio(self, session: VoiceSession, pcm: bytes) -> None:
        # 打断仲裁：回合进行中（生成或播报）时，人声能量立即取消
        if session.generation_id is not None and session.vad.is_voiced(pcm):
            await self._interrupt(session, reason="barge_in")
            return
        if session.collecting:
            session.utterance.extend(pcm)
        if session.ptt_active:
            return
        event = session.vad.feed(pcm)
        if event is None:
            return
        if event.kind == "utterance_started":
            session.collecting = True
            session.utterance.clear()
            session.utterance.extend(pcm)
        elif event.kind == "utterance_ended":
            await self._finalize_utterance(session)

    async def _finalize_utterance(self, session: VoiceSession) -> None:
        session.collecting = False
        session.vad.force_end()
        pcm = bytes(session.utterance)
        session.utterance.clear()
        if len(pcm) < MIN_UTTERANCE_BYTES:
            return
        # 每条话语解析一次提供方：管理端改语音配置即时生效，无需重启
        recognizer, tts_chain = await self._voice_source.resolve()
        if recognizer is None:
            await self._send(session, "voice.asr_unavailable", {"reason": "not_configured"})
            return
        # 隐私闸门：L2 音频禁止交给云端识别器出站
        if session.privacy_level is PrivacyLevel.L2 and not recognizer.runs_local:
            await self._send(session, "voice.asr_unavailable", {"reason": "local_asr_required"})
            return
        if session.conversation_id is None or session.turn_task is not None:
            return
        session.turn_task = asyncio.create_task(
            self._run_utterance(session, pcm, recognizer, tts_chain),
            name="voice-utterance",
        )

    async def _run_utterance(
        self,
        session: VoiceSession,
        pcm: bytes,
        recognizer: SpeechRecognizer,
        tts_chain: TtsProviderChain | None,
    ) -> None:
        if session.conversation_id is None:
            return
        started = time.perf_counter()
        transcript_at = first_token_at = first_audio_at = 0.0
        pending: PendingTurn | None = None
        try:
            transcript = await recognizer.transcribe(
                pcm, sample_rate=SUPPORTED_SAMPLE_RATE, language=None
            )
            transcript_at = time.perf_counter()
            if not transcript.strip():
                return
            await self._send(
                session, "voice.transcript", {"text": transcript, "is_final": True}
            )
            pending = await self._service.start_turn(
                session.conversation_id,
                user_id=session.principal.user_id,
                text=transcript,
                privacy_level=session.privacy_level,
            )
            generation_id = pending.generation_id
            session.generation_id = generation_id
            await self._send(
                session,
                "turn.accepted",
                {
                    "generation_id": str(generation_id),
                    "turn_id": str(pending.turn_id),
                    "privacy_level": session.privacy_level.value,
                },
            )
            buffer = SentenceBuffer()
            sentence_index = 0

            async def speak(sentence: str) -> None:
                nonlocal sentence_index, first_audio_at
                if tts_chain is None:
                    # 未配置任何 TTS：纯文字语音回合，只提示一次
                    if not session.tts_unavailable_notified:
                        session.tts_unavailable_notified = True
                        await self._send(
                            session, "voice.tts_unavailable", {"reason": "not_configured"}
                        )
                    return
                selection = await tts_chain.select(
                    sentence, privacy_level=session.privacy_level
                )
                await self._send(
                    session,
                    "voice.sentence",
                    {
                        "generation_id": str(generation_id),
                        "index": sentence_index,
                        "text": sentence,
                        "mime": selection.provider.mime,
                        "sample_rate": selection.provider.sample_rate,
                        "provider": type(selection.provider).__name__,
                    },
                )
                try:
                    await session.websocket.send_bytes(selection.first_chunk)
                    if first_audio_at == 0.0:
                        first_audio_at = time.perf_counter()
                    async for chunk in selection.stream:
                        await session.websocket.send_bytes(chunk)
                except Exception:
                    # 中途断流：该句音频残缺，冷却该提供方并跳句，回合继续
                    tts_chain.report_failure(selection.provider)
                    logger.warning(
                        "tts stream broken mid-sentence provider=%s",
                        type(selection.provider).__name__,
                        exc_info=True,
                    )
                await self._send(
                    session,
                    "voice.sentence.end",
                    {"generation_id": str(generation_id), "index": sentence_index},
                )
                sentence_index += 1

            async def on_delta(delta: str) -> None:
                nonlocal first_token_at
                if first_token_at == 0.0:
                    first_token_at = time.perf_counter()
                for sentence in buffer.push(delta):
                    await speak(sentence)

            turn = await self._service.run_stream(pending, on_delta)
            remainder = buffer.flush()
            if remainder:
                await speak(remainder)
            await self._send(
                session,
                "reply.committed",
                {
                    "generation_id": str(generation_id),
                    "message_id": str(turn.assistant_message.id),
                    "content": turn.assistant_message.content,
                },
            )
            self._log_metrics(
                session,
                generation_id,
                started,
                transcript_at,
                first_token_at,
                first_audio_at,
            )
        except (TurnCancelled, asyncio.CancelledError):
            if pending is not None:
                await self._send(
                    session,
                    "turn.cancelled",
                    {"generation_id": str(pending.generation_id)},
                )
        except LLMRouteExhausted as error:
            logger.error(
                "voice turn model route failed generation_id=%s reason=%s",
                pending.generation_id if pending else None,
                error.reason_code,
            )
            await self._send_failure(session, pending, error.reason_code)
        except EgressBlocked as error:
            logger.warning(
                "voice turn egress blocked generation_id=%s reason=%s",
                pending.generation_id if pending else None,
                error.reason_code,
            )
            await self._send_failure(session, pending, error.reason_code)
        except LocalOnlySynthesizerError:
            # 提供方链没有可用的本地 TTS：语音降级文字，回合继续
            if not session.tts_unavailable_notified:
                session.tts_unavailable_notified = True
                await self._send(
                    session, "voice.tts_unavailable", {"reason": "local_tts_required"}
                )
        finally:
            session.generation_id = None
            session.turn_task = None

    async def _interrupt(self, session: VoiceSession, *, reason: str) -> None:
        generation_id = session.generation_id
        task = session.turn_task
        if generation_id is None or task is None:
            return
        changed = await self._service.cancel_turn(
            generation_id, user_id=session.principal.user_id
        )
        if changed:
            task.cancel()
            await self._send(
                session,
                "voice.interrupted",
                {"generation_id": str(generation_id), "reason": reason},
            )

    async def disconnect(self, session: VoiceSession) -> None:
        task = session.turn_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("voice turn task failed on disconnect", exc_info=True)

    async def _send_failure(
        self, session: VoiceSession, pending: PendingTurn | None, reason_code: str
    ) -> None:
        await self._send(
            session,
            "turn.failed",
            {
                "generation_id": str(pending.generation_id) if pending else None,
                "reason_code": reason_code,
            },
        )

    async def _send(
        self, session: VoiceSession, event_type: str, payload: dict[str, Any]
    ) -> None:
        await session.websocket.send_text(
            json.dumps({"type": event_type, **payload}, ensure_ascii=False)
        )

    def _log_metrics(
        self,
        session: VoiceSession,
        generation_id: UUID,
        started: float,
        transcript_at: float,
        first_token_at: float,
        first_audio_at: float,
    ) -> None:
        """docs/03 M2.7 打点：ASR / 首 token / 首音频分段耗时（毫秒）。"""

        def ms(until: float) -> int | None:
            return int((until - started) * 1000) if until > 0 else None

        logger.info(
            "voice turn metrics generation_id=%s asr_ms=%s first_token_ms=%s "
            "first_audio_ms=%s total_ms=%s",
            generation_id,
            ms(transcript_at),
            ms(first_token_at),
            ms(first_audio_at),
            int((time.perf_counter() - started) * 1000),
        )


def create_voice_websocket_router(
    service: ChatService,
    auth_service: AuthService,
    *,
    voice_source: VoiceProviderSource,
) -> tuple[APIRouter, VoiceWebSocketManager]:
    router = APIRouter(tags=["voice-websocket"])
    manager = VoiceWebSocketManager(service, voice_source=voice_source)

    @router.websocket("/ws/voice")
    async def voice_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            async with asyncio.timeout(5):
                raw_auth = await websocket.receive_json()
            auth = AuthenticateFrame.model_validate(raw_auth)
            principal = await auth_service.authenticate(auth.access_token)
        except (TimeoutError, ValidationError, InvalidSession, WebSocketDisconnect):
            await websocket.close(code=4401, reason="authentication required")
            return
        session = VoiceSession(websocket=websocket, principal=principal)
        try:
            await manager.run(session)
        except WebSocketDisconnect:
            pass
        finally:
            await manager.disconnect(session)

    return router, manager
