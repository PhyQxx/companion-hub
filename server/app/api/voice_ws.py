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
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.auth import AuthService, ChatPrincipal, InvalidSession
from app.chat import ChatService, PendingTurn, TurnCancelled
from app.ids import uuid7
from app.llm import LLMRouteExhausted
from app.privacy import EgressBlocked
from app.runtime import TurnCoordinator
from app.schemas import PrivacyLevel
from app.tools import ClientLocation, ClientLocationPayload
from app.voice import (
    FasterWhisperRecognizer,
    PcmAmplitudeEnvelope,
    SentenceBuffer,
    SpeechRecognitionUnavailable,
    SpeechRecognizer,
    TtsProviderChain,
    VoiceActivityDetector,
    VoiceLatencyMetrics,
    VoiceLatencySample,
    VoiceProviderSource,
    WakeWordDetector,
    WakeWordUnavailable,
    create_default_vad,
    create_default_wake_word,
)
from app.voice.contracts import LocalOnlySynthesizerError

from .auth import ChatSessionGuard
from .chat_ws import AuthenticateFrame

logger = logging.getLogger(__name__)

SUPPORTED_FORMAT = "pcm_s16le"
SUPPORTED_SAMPLE_RATE = 16_000
SUPPORTED_CHANNELS = 1
MIN_UTTERANCE_BYTES = 4_800  # 150ms：短于该长度视为噪声丢弃
MAX_TEXT_LENGTH = 20_000
# 语音回合只携带最近几轮消息：首响延迟对上下文长度极其敏感（lite 模型
# 20 条历史时首句可达 14s），更早的上下文由记忆检索与历史召回按需补齐。
VOICE_CONTEXT_MESSAGES = 8


class SubmittedTextRecognizer:
    """Adapt trusted text input to the existing streamed reply + TTS turn pipeline."""

    runs_local = True

    def __init__(self, text: str) -> None:
        self._text = text

    async def transcribe(
        self, pcm: bytes, *, sample_rate: int, language: str | None
    ) -> str:
        del pcm, sample_rate, language
        return self._text


@dataclass(slots=True)
class VoiceSession:
    """一条语音连接的会话状态。"""

    websocket: WebSocket
    principal: ChatPrincipal
    conversation_id: UUID | None = None
    privacy_level: PrivacyLevel = PrivacyLevel.L1
    # 连接级临时位置(voice.hello/text.submit 携带, TTL 15 分钟, 仅内存)。
    location: ClientLocation | None = None
    vad: VoiceActivityDetector = field(default_factory=create_default_vad)
    wake_word: WakeWordDetector | None = field(default_factory=create_default_wake_word)
    wake_armed: bool = False
    ptt_active: bool = False
    collecting: bool = False
    utterance: bytearray = field(default_factory=bytearray)
    generation_id: UUID | None = None
    turn_id: UUID | None = None
    turn_committed: bool = False
    turn_task: asyncio.Task[None] | None = None
    tts_unavailable_notified: bool = False

    def resolve_location(self, payload: object) -> ClientLocation | None:
        """更新或复用连接缓存位置; 非法载荷忽略并保留原值, 不打断语音回合。"""
        if payload is not None:
            with suppress(ValidationError):
                self.location = ClientLocationPayload.model_validate(
                    payload
                ).to_client_location()
        return self.location


class VoiceWebSocketManager:
    def __init__(
        self,
        service: ChatService,
        *,
        voice_source: VoiceProviderSource,
        turn_coordinator: TurnCoordinator | None = None,
    ) -> None:
        self._service = service
        self._turns = turn_coordinator
        self._voice_source = voice_source
        self._latency_metrics = VoiceLatencyMetrics()
        self._sessions: dict[int, VoiceSession] = {}

    def latency_report(self) -> dict[str, object]:
        return self._latency_metrics.snapshot()

    def reset_latency_metrics(self) -> dict[str, object]:
        self._latency_metrics.clear()
        return self._latency_metrics.snapshot()

    async def run(self, session: VoiceSession) -> None:
        """连接主循环：JSON 控制 + 二进制音频。"""
        key = id(session)
        self._sessions[key] = session
        try:
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
        finally:
            self._sessions.pop(key, None)

    async def broadcast_proactive(
        self,
        user_id: UUID,
        text: str,
        *,
        privacy_level: PrivacyLevel,
    ) -> int:
        sessions = [
            session
            for session in self._sessions.values()
            if session.principal.user_id == user_id
            and session.conversation_id is not None
            and session.turn_task is None
            and _privacy_rank(privacy_level) <= _privacy_rank(session.privacy_level)
        ]
        results = await asyncio.gather(
            *(self._send_proactive(session, text, privacy_level) for session in sessions),
            return_exceptions=True,
        )
        return sum(result is True for result in results)

    async def _send_proactive(
        self,
        session: VoiceSession,
        text: str,
        privacy_level: PrivacyLevel,
    ) -> bool:
        generation_id = uuid7()
        await self._send(
            session,
            "proactive.committed",
            {
                "generation_id": str(generation_id),
                "content": text,
                "privacy_level": str(privacy_level),
            },
        )
        _, tts_chain = await self._voice_source.resolve()
        if tts_chain is None:
            return True
        try:
            selection = await tts_chain.select(text, privacy_level=privacy_level)
        except LocalOnlySynthesizerError:
            await self._send(
                session,
                "voice.tts_unavailable",
                {"reason": "local_tts_required"},
            )
            return True
        await self._send(
            session,
            "voice.sentence",
            {
                "generation_id": str(generation_id),
                "index": 0,
                "text": text,
                "mime": selection.provider.mime,
                "sample_rate": selection.provider.sample_rate,
                "provider": type(selection.provider).__name__,
            },
        )
        try:
            await session.websocket.send_bytes(selection.first_chunk)
            async for chunk in selection.stream:
                await session.websocket.send_bytes(chunk)
        except Exception:
            tts_chain.report_failure(selection.provider)
            logger.warning("proactive voice TTS failed", exc_info=True)
            return True
        await self._send(
            session,
            "voice.sentence.end",
            {"generation_id": str(generation_id), "index": 0},
        )
        return True

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
                session.wake_armed = True
                session.collecting = True
                session.utterance.clear()
            case "utterance.end":
                session.ptt_active = False
                await self._finalize_utterance(session)
            case "text.submit":
                await self._on_text_submit(session, frame)
            case "interrupt":
                await self._interrupt(session, reason="client_interrupt")

    async def _on_text_submit(
        self, session: VoiceSession, frame: dict[str, Any]
    ) -> None:
        raw_text = frame.get("text")
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        if not text or len(text) > MAX_TEXT_LENGTH:
            await self._send(session, "voice.error", {"reason": "invalid_text"})
            return
        if session.conversation_id is None:
            await self._send(
                session, "voice.error", {"reason": "conversation_id_required"}
            )
            return
        if session.turn_task is not None:
            await self._send(session, "voice.error", {"reason": "turn_in_progress"})
            return
        # 文字输入无需 ASR，但仍即时解析 TTS 配置，确保后台保存后立刻生效。
        _, tts_chain = await self._voice_source.resolve()
        session.resolve_location(frame.get("location"))
        session.turn_task = asyncio.create_task(
            self._run_utterance(
                session,
                b"",
                SubmittedTextRecognizer(text),
                tts_chain,
            ),
            name="voice-text-turn",
        )

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
        session.resolve_location(frame.get("location"))
        recognizer, tts_chain = await self._voice_source.resolve()
        asr_warmup_ms: int | None = None
        asr_unavailable_reason: str | None = None
        if isinstance(recognizer, FasterWhisperRecognizer):
            warmup_started = time.perf_counter()
            try:
                await recognizer.warmup()
            except SpeechRecognitionUnavailable as error:
                logger.warning("local voice asr warmup unavailable reason=%s", error.reason)
                recognizer = None
                asr_unavailable_reason = error.reason
            else:
                asr_warmup_ms = int((time.perf_counter() - warmup_started) * 1000)
        await self._send(
            session,
            "voice.ready",
            {
                "conversation_id": str(session.conversation_id),
                "asr_configured": recognizer is not None,
                "asr_runs_local": recognizer.runs_local if recognizer is not None else None,
                "asr_provider": type(recognizer).__name__ if recognizer is not None else None,
                "asr_warmup_ms": asr_warmup_ms,
                "asr_unavailable_reason": asr_unavailable_reason,
                "tts_configured": tts_chain is not None,
                "tts_provider_count": len(tts_chain.providers) if tts_chain is not None else 0,
                "vad_backend": session.vad.backend,
                "wake_word_configured": session.wake_word is not None,
                "wake_word_backend": (
                    session.wake_word.backend if session.wake_word is not None else None
                ),
            },
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
        if session.wake_word is not None and not session.wake_armed:
            try:
                if not session.wake_word.feed(pcm):
                    return
            except WakeWordUnavailable as error:
                logger.warning("wake word unavailable reason=%s", error.reason)
                session.wake_word = None
                await self._send(
                    session, "voice.wake_unavailable", {"reason": error.reason}
                )
            except Exception as error:
                logger.warning(
                    "wake word runtime failed error_type=%s",
                    type(error).__name__,
                    exc_info=True,
                )
                session.wake_word = None
                await self._send(
                    session, "voice.wake_unavailable", {"reason": "runtime_error"}
                )
            else:
                session.wake_armed = True
                await self._send(
                    session,
                    "voice.wake_detected",
                    {"backend": "openwakeword"},
                )
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
        if session.wake_word is not None:
            session.wake_word.reset()
            session.wake_armed = False
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
        lease_device_id: UUID | None = None
        session.turn_committed = False
        try:
            try:
                transcript = await recognizer.transcribe(
                    pcm, sample_rate=SUPPORTED_SAMPLE_RATE, language=None
                )
            except SpeechRecognitionUnavailable as error:
                logger.warning(
                    "voice asr unavailable provider=%s reason=%s",
                    type(recognizer).__name__,
                    error.reason,
                )
                await self._send(
                    session, "voice.asr_unavailable", {"reason": error.reason}
                )
                return
            except Exception as error:
                logger.error(
                    "voice asr provider failed provider=%s error_type=%s",
                    type(recognizer).__name__,
                    type(error).__name__,
                    exc_info=True,
                )
                await self._send(
                    session, "voice.asr_unavailable", {"reason": "provider_error"}
                )
                return
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
                max_context_messages=VOICE_CONTEXT_MESSAGES,
                client_location=session.location,
            )
            generation_id = pending.generation_id
            session.generation_id = generation_id
            session.turn_id = pending.turn_id
            # 状态机与音频租约
            if self._turns is not None:
                await self._turns.transition(pending.turn_id, 1, "thinking")
                # 获取音频输出租约（device_id 临时生成，后续与设备注册表对齐）
                lease_device_id = uuid7()
                await self._turns.acquire_audio_lease(
                    lease_device_id,
                    generation_id,
                    ttl_seconds=60,
                )
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
                envelope = (
                    PcmAmplitudeEnvelope(selection.provider.sample_rate)
                    if selection.provider.mime.startswith("audio/pcm")
                    else None
                )
                viseme_index = 0

                async def send_audio_chunk(chunk: bytes) -> None:
                    nonlocal first_audio_at, viseme_index
                    if envelope is not None:
                        for amplitude in envelope.push(chunk):
                            await self._send(
                                session,
                                "voice.viseme",
                                {
                                    "generation_id": str(generation_id),
                                    "sentence_index": sentence_index,
                                    "index": viseme_index,
                                    "amp": round(amplitude, 4),
                                    "offset_ms": viseme_index * envelope.window_ms,
                                    "duration_ms": envelope.window_ms,
                                },
                            )
                            viseme_index += 1
                    await session.websocket.send_bytes(chunk)
                    if first_audio_at == 0.0:
                        first_audio_at = time.perf_counter()

                try:
                    await send_audio_chunk(selection.first_chunk)
                    async for chunk in selection.stream:
                        await send_audio_chunk(chunk)
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
                # 文字与 TTS 共用同一份可见 delta。先把文字发给浏览器，再按句
                # 触发合成，避免 TTS 已开始播放时页面仍停留在旧消息列表。
                await self._send(
                    session,
                    "reply.delta",
                    {"generation_id": str(generation_id), "delta": delta},
                )
                for sentence in buffer.push(delta):
                    await speak(sentence)

            async def on_tool_event(tool_event: dict[str, object]) -> None:
                event_type = str(tool_event.get("type") or "tool.status")
                await self._send(
                    session,
                    event_type,
                    {
                        "generation_id": str(generation_id),
                        **{
                            key: value
                            for key, value in tool_event.items()
                            if key != "type"
                        },
                    },
                )

            turn = await self._service.run_stream(pending, on_delta, on_tool_event)
            session.turn_committed = True
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
            # 状态机：streaming -> completed
            if self._turns is not None:
                await self._turns.transition(pending.turn_id, 3, "completed")
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
                if self._turns is not None:
                    await self._turns.transition(pending.turn_id, 2, "cancelled", reason="user_cancelled")
                if not session.turn_committed:
                    await self._send(
                        session,
                        "turn.cancelled",
                        {"generation_id": str(pending.generation_id)},
                    )
        except LLMRouteExhausted as error:
            if pending is not None and self._turns is not None:
                await self._turns.transition(pending.turn_id, 2, "failed")
            logger.error(
                "voice turn model route failed generation_id=%s reason=%s",
                pending.generation_id if pending else None,
                error.reason_code,
            )
            await self._send_failure(session, pending, error.reason_code)
        except EgressBlocked as error:
            if pending is not None and self._turns is not None:
                await self._turns.transition(pending.turn_id, 2, "failed")
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
            session.turn_committed = False
            session.turn_task = None
            # 释放音频租约
            if self._turns is not None and lease_device_id is not None:
                await self._turns.release_audio_lease(lease_device_id)

    async def _interrupt(self, session: VoiceSession, *, reason: str) -> None:
        started = time.perf_counter()
        generation_id = session.generation_id
        task = session.turn_task
        if task is None:
            return
        if generation_id is None:
            await self._send(
                session,
                "voice.interrupted",
                {"generation_id": None, "reason": reason},
            )
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self._latency_metrics.record_interrupt(
                int((time.perf_counter() - started) * 1000)
            )
            return
        # 若配置了 TurnCoordinator，优先走状态机驱动的打断
        if self._turns is not None and session.turn_id is not None:
            await self._turns.interrupt(
                session.turn_id,
                user_id=session.principal.user_id,
                reason=reason,
            )
        changed = await self._service.cancel_turn(
            generation_id, user_id=session.principal.user_id
        )
        await self._send(
            session,
            "voice.interrupted",
            {
                "generation_id": str(generation_id),
                "reason": reason,
                "turn_cancelled": changed,
            },
        )
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        interrupt_ms = int((time.perf_counter() - started) * 1000)
        self._latency_metrics.record_interrupt(interrupt_ms)
        logger.info(
            "voice interrupt metrics generation_id=%s reason=%s interrupt_ms=%s",
            generation_id,
            reason,
            interrupt_ms,
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

        asr_ms = ms(transcript_at)
        first_token_ms = ms(first_token_at)
        first_audio_ms = ms(first_audio_at)
        total_ms = int((time.perf_counter() - started) * 1000)
        self._latency_metrics.record(
            VoiceLatencySample(
                asr_ms=asr_ms,
                first_token_ms=first_token_ms,
                first_audio_ms=first_audio_ms,
                total_ms=total_ms,
            )
        )
        logger.info(
            "voice turn metrics generation_id=%s asr_ms=%s first_token_ms=%s "
            "first_audio_ms=%s total_ms=%s",
            generation_id,
            asr_ms,
            first_token_ms,
            first_audio_ms,
            total_ms,
        )


def create_voice_websocket_router(
    service: ChatService,
    auth_service: AuthService,
    *,
    voice_source: VoiceProviderSource,
    turn_coordinator: TurnCoordinator | None = None,
    wake_word_factory: Callable[[], WakeWordDetector | None] = create_default_wake_word,
) -> tuple[APIRouter, VoiceWebSocketManager]:
    router = APIRouter(tags=["voice-websocket"])
    manager = VoiceWebSocketManager(
        service, voice_source=voice_source, turn_coordinator=turn_coordinator
    )

    @router.get("/api/v1/meta/voice/latency")
    async def voice_latency() -> dict[str, object]:
        return manager.latency_report()

    session_guard = ChatSessionGuard(auth_service)

    @router.post("/api/v1/meta/voice/latency/reset")
    async def reset_voice_latency(
        principal: ChatPrincipal = Depends(session_guard),  # noqa: B008
    ) -> dict[str, object]:
        del principal
        return manager.reset_latency_metrics()

    @router.websocket("/ws/voice")
    async def voice_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            async with asyncio.timeout(5):
                raw_auth = await websocket.receive_json()
            auth = AuthenticateFrame.model_validate(raw_auth)
            principal = await auth_service.authenticate(auth.access_token)
        except WebSocketDisconnect:
            return
        except (TimeoutError, ValidationError, InvalidSession):
            with suppress(WebSocketDisconnect):
                await websocket.close(code=4401, reason="authentication required")
            return
        session = VoiceSession(
            websocket=websocket,
            principal=principal,
            wake_word=wake_word_factory(),
        )
        try:
            await manager.run(session)
        except WebSocketDisconnect:
            pass
        finally:
            await manager.disconnect(session)

    return router, manager


def _privacy_rank(level: PrivacyLevel) -> int:
    ranks = {
        PrivacyLevel.L0: 0,
        PrivacyLevel.L1: 1,
        PrivacyLevel.L2: 2,
        PrivacyLevel.L3: 3,
    }
    return ranks[PrivacyLevel(level)]
