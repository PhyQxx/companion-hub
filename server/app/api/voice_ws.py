"""P6 语音通道（docs/04 §3.1）：/ws/voice 双向音频流。

上行：PCM16 音频二进制帧 + JSON 控制（hello / PTT 边界 / 打断）；
下行：转写、句级 TTS 音频分片（mime 按实际提供方声明）、回合生命周期
与打断事件。回合本体复用 ChatService（记忆、Timeline、隐私路由、取消
全部生效），语音层只做断句、转写、合成与打断仲裁。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import JsonValue, ValidationError

from app.auth import AuthService, ChatPrincipal, InvalidSession
from app.avatar import AvatarControlPublisher, control_from_agent_reply, with_reply_text
from app.chat import ChatService, PendingTurn, TurnCancelled
from app.ids import uuid7
from app.llm import LLMRoute, LLMRouteExhausted
from app.privacy import EgressBlocked
from app.runtime import TurnCoordinator
from app.schemas import PrivacyLevel
from app.tools import ClientLocation, ClientLocationPayload
from app.voice import (
    FasterWhisperRecognizer,
    MarkdownSpeechFilter,
    PcmAmplitudeEnvelope,
    SentenceBuffer,
    SpeechRecognitionUnavailable,
    SpeechRecognizer,
    StreamingSpeechRecognizer,
    TtsProviderChain,
    VoiceActivityDetector,
    VoiceLatencyMetrics,
    VoiceLatencySample,
    VoiceProviderSource,
    WakeWordDetector,
    WakeWordUnavailable,
    create_default_vad,
    create_default_wake_word,
    markdown_to_speech_text,
)
from app.voice.contracts import LocalOnlySynthesizerError

from .auth import ChatSessionGuard
from .chat_ws import AuthenticateFrame

logger = logging.getLogger(__name__)
WEBSOCKET_KEEPALIVE_SECONDS = 2.0

SUPPORTED_FORMAT = "pcm_s16le"
SUPPORTED_SAMPLE_RATE = 16_000
SUPPORTED_CHANNELS = 1
MIN_UTTERANCE_BYTES = 4_800  # 150ms：短于该长度视为噪声丢弃
# 连续对话麦克风常开，环境噪声的瞬时爆发也能触发 VAD；带 450ms hangover
# 尾巴仍不足 600ms 的话语只可能是单次噪声脉冲，直接丢弃。
MIN_AUTO_UTTERANCE_BYTES = 19_200  # 600ms @ 16kHz PCM16 mono
MAX_TEXT_LENGTH = 20_000
# 连续对话里 Whisper 对噪声的复读/回声会造成逐字相同的连续“话语”，
# 窗口内完全相同的转写只处理第一次。
DUPLICATE_TRANSCRIPT_WINDOW_SECONDS = 6.0
# 语音回合只携带最近几轮消息：首响延迟对上下文长度极其敏感（lite 模型
# 20 条历史时首句可达 14s），更早的上下文由记忆检索与历史召回按需补齐。
VOICE_CONTEXT_MESSAGES = 8
DEVICE_AUDIO_CHUNK_BYTES = 24 * 1024
DEVICE_AUDIO_MAX_BYTES = 8 * 1024 * 1024


@dataclass(slots=True)
class AsrPrefetch:
    """VAD hangover 期间提前执行的整段 ASR；恢复说话后立即作废。"""

    recognizer: SpeechRecognizer
    task: asyncio.Task[str]


class UtteranceStreamer:
    """话语级流式 ASR 句柄（docs/04 §6.4 P1）。

    说话期间逐帧 feed（同步推理经 to_thread），部分转写经
    voice.partial_transcript 下发；断句时 finalize 直接得到终稿，
    免去整段收尾等待。识别器不支持流式或 feed 阶段出错时自动停用，
    断句后回退整段转写（安全边界与 fallback 语义不变）。
    """

    def __init__(self, recognizer: SpeechRecognizer) -> None:
        self._recognizer: StreamingSpeechRecognizer | None = (
            recognizer if isinstance(recognizer, StreamingSpeechRecognizer) else None
        )
        self._last_partial = ""
        # P2 前置测量（docs/04 §6.4）：最后一段 partial 稳定（无变化）的起算点
        self.last_change_monotonic: float | None = None

    @property
    def last_partial(self) -> str | None:
        return self._last_partial or None

    @property
    def active(self) -> bool:
        return self._recognizer is not None

    async def feed(self, pcm: bytes) -> str | None:
        recognizer = self._recognizer
        if recognizer is None:
            return None
        try:
            partial = await asyncio.to_thread(recognizer.feed, pcm)
        except Exception:
            logger.warning(
                "voice streaming asr feed failed provider=%s; falling back to full transcription",
                type(recognizer).__name__,
                exc_info=True,
            )
            self._recognizer = None
            return None
        if partial is None:
            return None
        stripped = partial.strip()
        if not stripped or stripped == self._last_partial:
            return None
        self._last_partial = stripped
        self.last_change_monotonic = time.monotonic()
        return stripped

    async def finalize(self) -> str | None:
        """断句收尾：返回终稿；失败或未激活返回 None（回退整段转写）。"""

        recognizer, self._recognizer = self._recognizer, None
        if recognizer is None:
            return None
        try:
            final = await asyncio.to_thread(recognizer.finalize)
        except Exception:
            logger.warning(
                "voice streaming asr finalize failed provider=%s; falling back",
                type(recognizer).__name__,
                exc_info=True,
            )
            return None
        return final.strip() or None


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


class SatelliteVoiceSocket:
    """把现有语音 WebSocket 输出封装成设备通道的签名 JSON 帧。"""

    def __init__(self, emit: Callable[[str, dict[str, JsonValue]], Awaitable[None]]) -> None:
        self._emit = emit
        self._sentence_index = 0
        self._chunk_index = 0
        self.completed = False

    async def send_text(self, value: str) -> None:
        frame = json.loads(value)
        if not isinstance(frame, dict):
            return
        frame_type = str(frame.pop("type", "voice.event"))
        if frame_type == "voice.sentence":
            raw_index = frame.get("index")
            self._sentence_index = raw_index if isinstance(raw_index, int) else 0
            self._chunk_index = 0
        elif frame_type == "reply.committed":
            self.completed = True
        await self._emit(frame_type, frame)

    async def send_bytes(self, value: bytes) -> None:
        for offset in range(0, len(value), DEVICE_AUDIO_CHUNK_BYTES):
            chunk = value[offset : offset + DEVICE_AUDIO_CHUNK_BYTES]
            await self._emit(
                "satellite.audio.chunk",
                {
                    "sentence_index": self._sentence_index,
                    "index": self._chunk_index,
                    "data_b64": base64.b64encode(chunk).decode("ascii"),
                },
            )
            self._chunk_index += 1

    async def close(self, **_: object) -> None:
        return None


@dataclass(slots=True)
class VoiceSession:
    """一条语音连接的会话状态。"""

    websocket: WebSocket
    principal: ChatPrincipal
    # 连接级稳定设备身份：多端租约仲裁的持有者标识（同一连接跨回合不变）。
    device_id: UUID = field(default_factory=uuid7)
    conversation_id: UUID | None = None
    privacy_level: PrivacyLevel = PrivacyLevel.L1
    # 连接级临时位置(voice.hello/text.submit 携带, TTL 15 分钟, 仅内存)。
    location: ClientLocation | None = None
    vad: VoiceActivityDetector = field(default_factory=create_default_vad)
    wake_word: WakeWordDetector | None = field(default_factory=create_default_wake_word)
    wake_armed: bool = False
    # 连续对话（电话模式）：麦克风常开，自动 VAD 断句即发送、说话即打断，
    # 且跳过唤醒词待命门——通话中不应每句话都重新唤醒。
    continuous: bool = False
    ptt_active: bool = False
    collecting: bool = False
    utterance: bytearray = field(default_factory=bytearray)
    generation_id: UUID | None = None
    turn_id: UUID | None = None
    turn_committed: bool = False
    turn_task: asyncio.Task[None] | None = None
    asr_prefetch: AsrPrefetch | None = None
    # 流式 ASR 句柄（docs/04 §6.4 P1）：话语开始时绑定识别器，逐帧喂入；
    # 识别器不具备流式能力时为 None，走原整段转写路径。
    utterance_streamer: UtteranceStreamer | None = None
    tts_unavailable_notified: bool = False
    last_transcript: str | None = None
    last_transcript_at: float | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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
        avatar_control_publisher: AvatarControlPublisher | None = None,
        vad_factory: Callable[[], VoiceActivityDetector] = create_default_vad,
    ) -> None:
        self._service = service
        self._turns = turn_coordinator
        self._voice_source = voice_source
        self._avatar_control = avatar_control_publisher
        self._vad_factory = vad_factory
        self._avatar_tasks: set[asyncio.Task[int]] = set()
        self._latency_metrics = VoiceLatencyMetrics()
        self._sessions: dict[int, VoiceSession] = {}
        self._satellite_conversations: dict[tuple[UUID, UUID], UUID] = {}
        self._satellite_conversation_locks: dict[tuple[UUID, UUID], asyncio.Lock] = {}
        self._satellite_broadcaster: Callable[..., Awaitable[int]] | None = None

    def set_satellite_broadcaster(
        self, broadcaster: Callable[..., Awaitable[int]]
    ) -> None:
        self._satellite_broadcaster = broadcaster

    def latency_report(self) -> dict[str, object]:
        return self._latency_metrics.snapshot()

    def reset_latency_metrics(self) -> dict[str, object]:
        self._latency_metrics.clear()
        return self._latency_metrics.snapshot()

    # ------------------------------------------------------------------
    # 多端租约仲裁：同类型租约"最新获取者抢占，旧持有者尽快停止"。
    # ------------------------------------------------------------------

    def _session_by_device(self, device_id: UUID) -> VoiceSession | None:
        for session in self._sessions.values():
            if session.device_id == device_id:
                return session
        return None

    async def _preempt_audio_holder(
        self, previous_holder: UUID, *, exclude: VoiceSession | None
    ) -> None:
        """新回合抢占音频输出后，通知旧持有者并打断其进行中的回合。"""
        other = self._session_by_device(previous_holder)
        if other is None or other is exclude:
            return
        try:
            if other.turn_task is not None:
                await self._send(
                    other,
                    "voice.audio_preempted",
                    {
                        "generation_id": str(other.generation_id)
                        if other.generation_id
                        else None,
                        "by_device": str(exclude.device_id) if exclude else None,
                    },
                )
                await self._interrupt(other, reason="audio_lease_preempted")
        except Exception:
            logger.warning(
                "audio lease preemption notify failed device=%s", previous_holder, exc_info=True
            )

    async def _acquire_microphone(self, session: VoiceSession) -> None:
        """话语采集开始时获取麦克风租约；抢占其他正在采集的连接。"""
        if self._turns is None:
            return
        try:
            result = await self._turns.acquire_microphone(session.device_id, ttl_seconds=120)
        except Exception:
            logger.warning("microphone lease acquire failed", exc_info=True)
            return
        if result.previous_holder is None or result.previous_holder == session.device_id:
            return
        other = self._session_by_device(result.previous_holder)
        if other is None or other is session:
            return
        other.collecting = False
        other.ptt_active = False
        other.wake_armed = False
        other.utterance.clear()
        other.utterance_streamer = None
        self._discard_asr_prefetch(other)
        try:
            await self._send(
                other,
                "voice.microphone_preempted",
                {"by_device": str(session.device_id)},
            )
        except Exception:
            logger.warning("microphone preemption notify failed", exc_info=True)

    async def _release_microphone(self, session: VoiceSession) -> None:
        if self._turns is None:
            return
        try:
            await self._turns.release_microphone(session.device_id)
        except Exception:
            logger.warning("microphone lease release failed", exc_info=True)

    async def stream_device_speech(
        self,
        text: str,
        privacy_level: PrivacyLevel,
        emit: Callable[[str, dict[str, JsonValue]], Awaitable[None]],
    ) -> bool:
        """通过设备签名帧投递一段 TTS；L2 仍由 provider chain 强制本地。

        播报期间持有 audio_output 租约：语音回合随时可抢占，被抢占时
        停止发送剩余音频块（桌面端按 failed 终止播放）。
        """
        _, tts_chain = await self._voice_source.resolve()
        if tts_chain is None:
            await emit("pet.audio.failed", {"reason_code": "tts_not_configured"})
            return False
        speech_text = markdown_to_speech_text(text)
        if not speech_text:
            await emit("pet.audio.failed", {"reason_code": "tts_empty_text"})
            return False
        try:
            selection = await tts_chain.select(speech_text, privacy_level=privacy_level)
        except LocalOnlySynthesizerError:
            await emit("pet.audio.failed", {"reason_code": "local_tts_required"})
            return False
        except Exception:
            await emit("pet.audio.failed", {"reason_code": "tts_generation_failed"})
            return False
        provider = selection.provider
        lease_device: UUID | None = None
        if self._turns is not None:
            try:
                lease_device = uuid7()
                lease = await self._turns.acquire_audio_lease(
                    lease_device, uuid7(), ttl_seconds=120
                )
                if lease.previous_holder is not None:
                    await self._preempt_audio_holder(lease.previous_holder, exclude=None)
            except Exception:
                logger.warning("pet audio lease acquire failed", exc_info=True)
                lease_device = None
        await emit(
            "pet.audio.start",
            {
                "mime": provider.mime,
                "sample_rate": provider.sample_rate,
                "provider": type(provider).__name__,
            },
        )
        index = 0
        total_bytes = 0
        try:
            async for chunk in _prepend_audio_chunk(
                selection.first_chunk, selection.stream
            ):
                if lease_device is not None and not await self._still_holds_audio(
                    lease_device
                ):
                    await emit(
                        "pet.audio.failed", {"reason_code": "audio_preempted"}
                    )
                    return False
                for offset in range(0, len(chunk), DEVICE_AUDIO_CHUNK_BYTES):
                    part = chunk[offset : offset + DEVICE_AUDIO_CHUNK_BYTES]
                    if total_bytes + len(part) > DEVICE_AUDIO_MAX_BYTES:
                        await emit(
                            "pet.audio.failed",
                            {"reason_code": "tts_audio_too_large"},
                        )
                        return False
                    total_bytes += len(part)
                    await emit(
                        "pet.audio.chunk",
                        {
                            "index": index,
                            "data_b64": base64.b64encode(part).decode("ascii"),
                        },
                    )
                    index += 1
        except Exception:
            tts_chain.report_failure(provider)
            await emit("pet.audio.failed", {"reason_code": "tts_stream_failed"})
            return False
        finally:
            if lease_device is not None and self._turns is not None:
                with suppress(Exception):
                    await self._turns.release_audio_lease(lease_device)
        await emit(
            "pet.audio.end",
            {"chunks": index, "bytes": total_bytes},
        )
        return True

    async def run_satellite_utterance(
        self,
        owner_user_id: UUID,
        device_id: UUID,
        pcm: bytes,
        privacy_level: PrivacyLevel,
        emit: Callable[[str, dict[str, JsonValue]], Awaitable[None]],
    ) -> bool:
        """复用浏览器语音回合处理卫星 PCM，并把输出回送为签名 JSON 帧。"""
        key = (owner_user_id, device_id)
        conversation_id = self._satellite_conversations.get(key)
        if conversation_id is None:
            lock = self._satellite_conversation_locks.setdefault(key, asyncio.Lock())
            async with lock:
                conversation_id = self._satellite_conversations.get(key)
                if conversation_id is None:
                    conversation = await self._service.create_conversation(
                        user_id=owner_user_id,
                        title="房间语音",
                    )
                    conversation_id = conversation.id
                    self._satellite_conversations[key] = conversation_id
            self._satellite_conversation_locks.pop(key, None)

        recognizer, tts_chain = await self._voice_source.resolve()
        if recognizer is None:
            await emit("voice.asr_unavailable", {"reason": "not_configured"})
            return False
        if privacy_level is PrivacyLevel.L2 and not recognizer.runs_local:
            await emit("voice.asr_unavailable", {"reason": "local_asr_required"})
            return False

        socket = SatelliteVoiceSocket(emit)
        session = VoiceSession(
            websocket=cast(WebSocket, socket),
            principal=ChatPrincipal(
                session_id=uuid7(),
                user_id=owner_user_id,
                display_name="Satellite",
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            ),
            device_id=device_id,
            conversation_id=conversation_id,
            privacy_level=privacy_level,
            wake_word=None,
            vad=self._vad_factory(),
        )
        session.turn_task = asyncio.current_task()
        session_key = id(session)
        self._sessions[session_key] = session
        try:
            await self._run_utterance(session, pcm, recognizer, tts_chain)
        finally:
            self._sessions.pop(session_key, None)
        return socket.completed

    async def transfer_satellite_conversation(
        self,
        owner_user_id: UUID,
        from_device_id: UUID,
        to_device_id: UUID,
    ) -> None:
        """仅在网关完成同 owner 接管校验后，把连续上下文迁到新房间。"""
        source_key = (owner_user_id, from_device_id)
        target_key = (owner_user_id, to_device_id)
        conversation_id = self._satellite_conversations.pop(source_key, None)
        if conversation_id is not None:
            self._satellite_conversations[target_key] = conversation_id

    async def _still_holds_audio(self, device_id: UUID) -> bool:
        assert self._turns is not None
        holder = await self._turns.current_audio_holder()
        return holder is not None and holder == device_id

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
        delivered = sum(result is True for result in results)
        if self._satellite_broadcaster is not None:
            try:
                delivered += await self._satellite_broadcaster(
                    user_id,
                    text,
                    privacy_level=privacy_level,
                )
            except Exception:
                logger.warning("satellite proactive broadcast failed", exc_info=True)
        return delivered

    async def _send_proactive(
        self,
        session: VoiceSession,
        text: str,
        privacy_level: PrivacyLevel,
    ) -> bool:
        generation_id = uuid7()
        if privacy_level in {PrivacyLevel.L0, PrivacyLevel.L1}:
            self._schedule_avatar_control(
                session.principal.user_id, with_reply_text({}, text)
            )
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
        speech_text = markdown_to_speech_text(text)
        if not speech_text:
            return True
        try:
            selection = await tts_chain.select(speech_text, privacy_level=privacy_level)
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
                "text": speech_text,
                "mime": selection.provider.mime,
                "sample_rate": selection.provider.sample_rate,
                "provider": type(selection.provider).__name__,
            },
        )
        try:
            await self._send_bytes(session, selection.first_chunk)
            async for chunk in selection.stream:
                await self._send_bytes(session, chunk)
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
                self._discard_asr_prefetch(session)
                session.ptt_active = True
                session.wake_armed = True
                session.collecting = True
                session.utterance.clear()
                await self._start_streamer(session, None)
                await self._acquire_microphone(session)
            case "utterance.end":
                session.ptt_active = False
                await self._finalize_utterance(session, explicit=True)
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
        session.continuous = frame.get("continuous") is True
        session.resolve_location(frame.get("location"))
        # Silero/torch 初始化较重，连接建立即后台预热，不落在第一句话上。
        warmup = getattr(session.vad, "warmup", None)
        if callable(warmup):
            asyncio.get_running_loop().create_task(self._warm_vad(session))
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

    async def _warm_vad(self, session: VoiceSession) -> None:
        warmup = getattr(session.vad, "warmup", None)
        if not callable(warmup):
            return
        try:
            await asyncio.to_thread(warmup)
        except Exception:
            # 预热失败不打断连接；首次喂帧时按既有降级逻辑回退能量 VAD。
            logger.warning("voice vad warmup failed; lazy load on first frame", exc_info=True)

    async def _on_audio(self, session: VoiceSession, pcm: bytes) -> None:
        # 打断仲裁：回合进行中（生成或播报）时，人声能量立即取消
        if session.generation_id is not None and session.vad.is_voiced(pcm):
            await self._interrupt(session, reason="barge_in")
            return
        was_speaking = session.vad.speaking
        voiced = session.vad.is_voiced(pcm)
        if session.collecting:
            session.utterance.extend(pcm)
            await self._feed_streamer(session, pcm)
        if session.ptt_active:
            return
        if session.wake_word is not None and not session.continuous and not session.wake_armed:
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
        # 自动断句会等待约 450ms 静音。首个静音帧到达后立刻预取 ASR，
        # 把这段本来纯等待的 hangover 与识别耗时重叠起来；若用户继续说，
        # 预取结果立即作废，最终仍走完整音频转写。流式识别器说话期间
        # 已在逐帧喂入，无需（也不会）整段预取。
        if (
            session.collecting
            and was_speaking
            and not (session.utterance_streamer and session.utterance_streamer.active)
        ):
            if voiced:
                self._discard_asr_prefetch(session)
            elif session.asr_prefetch is None:
                await self._start_asr_prefetch(session)
        event = session.vad.feed(pcm)
        if event is None:
            return
        if event.kind == "utterance_started":
            self._discard_asr_prefetch(session)
            session.collecting = True
            session.utterance.clear()
            session.utterance.extend(pcm)
            await self._start_streamer(session, pcm)
            await self._acquire_microphone(session)
        elif event.kind == "utterance_ended":
            await self._finalize_utterance(session, explicit=False)

    async def _start_streamer(self, session: VoiceSession, first_pcm: bytes | None) -> None:
        """话语开始时绑定流式识别器并喂入首帧；不具备流式能力则保持 None。"""

        session.utterance_streamer = None
        try:
            recognizer, _tts = await self._voice_source.resolve()
        except Exception:
            logger.warning("voice streamer resolve failed", exc_info=True)
            return
        if recognizer is None or not isinstance(recognizer, StreamingSpeechRecognizer):
            return
        streamer = UtteranceStreamer(recognizer)
        if not streamer.active:
            return
        session.utterance_streamer = streamer
        if first_pcm is not None:
            await self._feed_streamer(session, first_pcm)

    async def _feed_streamer(self, session: VoiceSession, pcm: bytes) -> None:
        streamer = session.utterance_streamer
        if streamer is None or not streamer.active:
            return
        partial = await streamer.feed(pcm)
        if partial is not None:
            await self._send(session, "voice.partial_transcript", {"text": partial})

    async def _finalize_streamer(
        self, session: VoiceSession
    ) -> tuple[str | None, int | None, str | None]:
        """断句时收尾流式识别。

        返回 (终稿, 断句前最后一段 partial 的稳定毫秒数, partial 与终稿的
        关系)。终稿为 None 表示未激活/失败（回退整段转写）；停顿与匹配统计
        独立于终稿成败，供 P2 投机可行性测量使用。
        """

        streamer, session.utterance_streamer = session.utterance_streamer, None
        if streamer is None:
            return None, None, None
        final = await streamer.finalize()
        stable_ms: int | None = None
        if streamer.last_change_monotonic is not None:
            stable_ms = max(
                0, int((time.monotonic() - streamer.last_change_monotonic) * 1000)
            )
        match: str | None = None
        last_partial = streamer.last_partial
        if final and last_partial:
            if final == last_partial:
                match = "exact"
            elif final.startswith(last_partial):
                match = "prefix"
            else:
                match = "diverged"
        return final, stable_ms, match

    async def _finalize_utterance(self, session: VoiceSession, *, explicit: bool) -> None:
        session.collecting = False
        await self._release_microphone(session)
        session.vad.force_end()
        if session.wake_word is not None:
            session.wake_word.reset()
            session.wake_armed = False
        pcm = bytes(session.utterance)
        session.utterance.clear()
        # PTT 是明确的用户意图，保留短话语；自动断句按更长下限滤噪。
        min_bytes = MIN_UTTERANCE_BYTES if explicit else MIN_AUTO_UTTERANCE_BYTES
        if len(pcm) < min_bytes:
            self._discard_asr_prefetch(session)
            session.utterance_streamer = None
            return
        # 流式识别：断句前逐帧喂入已完成转写，finalize 直接取终稿
        streamed_transcript, streaming_pause_ms, partial_match = (
            await self._finalize_streamer(session)
        )
        # 每条话语解析一次提供方：管理端改语音配置即时生效，无需重启
        recognizer, tts_chain = await self._voice_source.resolve()
        if recognizer is None:
            await self._send(session, "voice.asr_unavailable", {"reason": "not_configured"})
            return
        # 隐私闸门：L2 音频禁止交给云端识别器出站
        if session.privacy_level is PrivacyLevel.L2 and not recognizer.runs_local:
            self._discard_asr_prefetch(session)
            await self._send(session, "voice.asr_unavailable", {"reason": "local_asr_required"})
            return
        if session.conversation_id is None:
            self._discard_asr_prefetch(session)
            return
        if session.turn_task is not None:
            # 连续对话里新话语即打断：上一回合还卡在 ASR/生成间隙时，用户
            # 又开口了，应取消旧回合并处理新话语，而不是静默丢弃。
            if not session.continuous:
                self._discard_asr_prefetch(session)
                return
            await self._interrupt(session, reason="barge_in")
        prefetched_transcript = streamed_transcript
        if prefetched_transcript is None:
            prefetched_transcript = await self._consume_asr_prefetch(session, recognizer)
        session.turn_task = asyncio.create_task(
            self._run_utterance(
                session,
                pcm,
                recognizer,
                tts_chain,
                prefetched_transcript=prefetched_transcript,
                streaming_pause_ms=streaming_pause_ms,
                partial_match=partial_match,
            ),
            name="voice-utterance",
        )

    async def _start_asr_prefetch(self, session: VoiceSession) -> None:
        if len(session.utterance) < MIN_UTTERANCE_BYTES:
            return
        recognizer, _ = await self._voice_source.resolve()
        if recognizer is None:
            return
        if session.privacy_level is PrivacyLevel.L2 and not recognizer.runs_local:
            return
        pcm = bytes(session.utterance)
        task = asyncio.create_task(
            recognizer.transcribe(
                pcm,
                sample_rate=SUPPORTED_SAMPLE_RATE,
                language=None,
            ),
            name="voice-asr-prefetch",
        )
        session.asr_prefetch = AsrPrefetch(recognizer=recognizer, task=task)

    async def _consume_asr_prefetch(
        self,
        session: VoiceSession,
        recognizer: SpeechRecognizer,
    ) -> str | None:
        prefetch, session.asr_prefetch = session.asr_prefetch, None
        if prefetch is None:
            return None
        if prefetch.recognizer is not recognizer:
            prefetch.task.cancel()
            return None
        try:
            transcript = await prefetch.task
        except asyncio.CancelledError:
            return None
        except Exception:
            logger.warning("voice ASR prefetch failed; falling back", exc_info=True)
            return None
        return transcript if transcript.strip() else None

    @staticmethod
    def _discard_asr_prefetch(session: VoiceSession) -> None:
        prefetch, session.asr_prefetch = session.asr_prefetch, None
        if prefetch is not None:
            if prefetch.task.done():
                with suppress(asyncio.CancelledError, Exception):
                    prefetch.task.result()
            else:
                prefetch.task.cancel()

    async def _run_utterance(
        self,
        session: VoiceSession,
        pcm: bytes,
        recognizer: SpeechRecognizer,
        tts_chain: TtsProviderChain | None,
        *,
        prefetched_transcript: str | None = None,
        streaming_pause_ms: int | None = None,
        partial_match: str | None = None,
    ) -> None:
        if session.conversation_id is None:
            return
        started = time.perf_counter()
        transcript_at = first_token_at = first_audio_at = 0.0
        pending: PendingTurn | None = None
        keepalive_task: asyncio.Task[None] | None = None
        session.turn_committed = False
        try:
            try:
                transcript = prefetched_transcript
                if transcript is None:
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
            # 连续对话：噪声触发的幻觉/回声往往逐字重复，短窗口内完全
            # 相同的转写只处理第一次（文字输入与卫星设备不受影响）。
            now = time.monotonic()
            if (
                session.continuous
                and transcript == session.last_transcript
                and session.last_transcript_at is not None
                and now - session.last_transcript_at
                < DUPLICATE_TRANSCRIPT_WINDOW_SECONDS
            ):
                logger.info(
                    "voice duplicate transcript dropped generation_window_s=%.1f",
                    now - session.last_transcript_at,
                )
                return
            session.last_transcript = transcript
            session.last_transcript_at = now
            await self._send(
                session,
                "voice.transcript",
                {
                    "text": transcript,
                    "is_final": True,
                    "asr_prefetched": prefetched_transcript is not None,
                },
            )
            pending = await self._service.start_turn(
                session.conversation_id,
                user_id=session.principal.user_id,
                text=transcript,
                privacy_level=session.privacy_level,
                max_context_messages=VOICE_CONTEXT_MESSAGES,
                client_location=session.location,
                llm_route=LLMRoute.VOICE,
            )
            generation_id = pending.generation_id
            session.generation_id = generation_id
            session.turn_id = pending.turn_id
            # 状态机与音频租约：连接级 device_id 作为持有者，抢占旧持有者并打断其回合
            if self._turns is not None:
                await self._turns.transition(pending.turn_id, 1, "thinking")
                lease = await self._turns.acquire_audio_lease(
                    session.device_id,
                    generation_id,
                    ttl_seconds=60,
                )
                if lease.previous_holder is not None:
                    await self._preempt_audio_holder(
                        lease.previous_holder, exclude=session
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
            keepalive_task = asyncio.create_task(
                self._keep_connection_alive(session, generation_id),
                name=f"voice-keepalive-{generation_id}",
            )
            buffer = SentenceBuffer(
                first_chunk_chars=pending.config.voice.first_tts_chunk_chars
            )
            speech_filter = MarkdownSpeechFilter()
            sentence_index = 0
            # 音频输出被抢占后本回合剩余句子降级为纯文字（delta 仍在发送）
            audio_lost = False

            async def speak(sentence: str) -> None:
                nonlocal sentence_index, first_audio_at, audio_lost
                sentence = speech_filter.clean(sentence)
                if not sentence:
                    return
                if not audio_lost and self._turns is not None:
                    try:
                        renewal = await self._turns.renew_audio_lease(
                            session.device_id, ttl_seconds=60
                        )
                        if not renewal.acquired:
                            audio_lost = True
                            await self._send(
                                session,
                                "voice.audio_preempted",
                                {"generation_id": str(generation_id), "by_device": None},
                            )
                    except Exception:
                        logger.warning("audio lease renew failed", exc_info=True)
                if audio_lost:
                    return
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
                    await self._send_bytes(session, chunk)
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
            reply_meta = (turn.assistant_message.decision_meta or {}).get("agent_reply")
            avatar_control = control_from_agent_reply(reply_meta)
            if session.privacy_level in {PrivacyLevel.L0, PrivacyLevel.L1}:
                avatar_control = with_reply_text(
                    avatar_control, turn.assistant_message.content
                )
            self._schedule_avatar_control(session.principal.user_id, avatar_control)
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
                prefetched_transcript is not None,
                streaming_pause_ms=streaming_pause_ms,
                partial_match=partial_match,
            )
        except (TurnCancelled, asyncio.CancelledError):
            if pending is not None:
                if self._turns is not None:
                    await self._turns.transition(
                        pending.turn_id, 2, "cancelled", reason="user_cancelled"
                    )
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
            if keepalive_task is not None:
                keepalive_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await keepalive_task
            session.generation_id = None
            session.turn_committed = False
            session.turn_task = None
            # 释放音频租约（仅当仍是持有者时生效）
            if self._turns is not None:
                try:
                    await self._turns.release_audio_lease(session.device_id)
                except Exception:
                    logger.warning("audio lease release failed", exc_info=True)

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
        self._discard_asr_prefetch(session)
        await self._release_microphone(session)
        task = session.turn_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("voice turn task failed on disconnect", exc_info=True)
        if self._turns is not None:
            with suppress(Exception):
                await self._turns.release_audio_lease(session.device_id)

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
        async with session.send_lock:
            await session.websocket.send_text(
                json.dumps({"type": event_type, **payload}, ensure_ascii=False)
            )
        control: dict[str, JsonValue] = {}
        if event_type == "voice.sentence":
            control = {"speaking": True, "lipSyncMilli": 0}
        elif event_type == "voice.viseme":
            amplitude = payload.get("amp")
            if isinstance(amplitude, int | float):
                control = {
                    "speaking": True,
                    "lipSyncMilli": round(max(0.0, min(1.0, amplitude)) * 1000),
                }
        elif event_type in {
            "voice.sentence.end",
            "voice.interrupted",
            "turn.cancelled",
            "turn.failed",
        }:
            control = {"speaking": False, "lipSyncMilli": 0}
        self._schedule_avatar_control(session.principal.user_id, control)

    @staticmethod
    async def _send_bytes(session: VoiceSession, chunk: bytes) -> None:
        async with session.send_lock:
            await session.websocket.send_bytes(chunk)

    async def _keep_connection_alive(
        self, session: VoiceSession, generation_id: UUID
    ) -> None:
        """Keep slow first-token and TTS work alive through short-idle proxies."""
        while True:
            await asyncio.sleep(WEBSOCKET_KEEPALIVE_SECONDS)
            await self._send(
                session,
                "connection.keepalive",
                {"generation_id": str(generation_id)},
            )

    def _schedule_avatar_control(
        self, owner_user_id: UUID, control: dict[str, JsonValue]
    ) -> None:
        if self._avatar_control is None or not control:
            return
        task = asyncio.create_task(
            self._avatar_control.publish_avatar_control(owner_user_id, control),
            name="voice-avatar-control",
        )
        self._avatar_tasks.add(task)
        task.add_done_callback(self._discard_avatar_task)

    def _discard_avatar_task(self, task: asyncio.Task[int]) -> None:
        self._avatar_tasks.discard(task)
        with suppress(Exception):
            task.result()

    def _log_metrics(
        self,
        session: VoiceSession,
        generation_id: UUID,
        started: float,
        transcript_at: float,
        first_token_at: float,
        first_audio_at: float,
        asr_prefetched: bool,
        *,
        streaming_pause_ms: int | None = None,
        partial_match: str | None = None,
    ) -> None:
        """docs/00 M2.7 打点：ASR / 首 token / 首音频分段耗时（毫秒）。

        streaming_pause_ms / partial_match 是 docs/04 §6.4 P2 的前置测量：
        断句前最后一段流式 partial 的稳定时长（句中停顿）与终稿匹配关系，
        用于以真实说话模式评估投机启动的可行提前量与采用率。
        """

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
                asr_prefetched=asr_prefetched,
                stable_partial_ms=streaming_pause_ms,
                partial_match=partial_match,
            )
        )
        logger.info(
            "voice turn metrics generation_id=%s asr_ms=%s first_token_ms=%s "
            "first_audio_ms=%s total_ms=%s asr_prefetched=%s "
            "stable_partial_ms=%s partial_match=%s",
            generation_id,
            asr_ms,
            first_token_ms,
            first_audio_ms,
            total_ms,
            asr_prefetched,
            streaming_pause_ms,
            partial_match,
        )


def create_voice_websocket_router(
    service: ChatService,
    auth_service: AuthService,
    *,
    voice_source: VoiceProviderSource,
    turn_coordinator: TurnCoordinator | None = None,
    wake_word_factory: Callable[[], WakeWordDetector | None] = create_default_wake_word,
    avatar_control_publisher: AvatarControlPublisher | None = None,
    vad_factory: Callable[[], VoiceActivityDetector] = create_default_vad,
) -> tuple[APIRouter, VoiceWebSocketManager]:
    router = APIRouter(tags=["voice-websocket"])
    manager = VoiceWebSocketManager(
        service,
        voice_source=voice_source,
        turn_coordinator=turn_coordinator,
        avatar_control_publisher=avatar_control_publisher,
        vad_factory=vad_factory,
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
            vad=manager._vad_factory(),
        )
        try:
            await manager.run(session)
        except WebSocketDisconnect:
            pass
        finally:
            await manager.disconnect(session)

    return router, manager


async def _prepend_audio_chunk(
    first_chunk: bytes, stream: AsyncIterator[bytes]
) -> AsyncIterator[bytes]:
    yield first_chunk
    async for chunk in stream:
        yield chunk


def _privacy_rank(level: PrivacyLevel) -> int:
    ranks = {
        PrivacyLevel.L0: 0,
        PrivacyLevel.L1: 1,
        PrivacyLevel.L2: 2,
        PrivacyLevel.L3: 3,
    }
    return ranks[PrivacyLevel(level)]
