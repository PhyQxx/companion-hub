# ruff: noqa: RUF002
"""语音管线契约：VAD / ASR / TTS 的提供方中立协议（对齐 LLMProvider 模式）。

实现方可以随时替换（能量 VAD → silero-vad、云端 MiMo → 本地
faster-whisper / GPT-SoVITS），调用侧只依赖协议与隐私语义。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from app.schemas import PrivacyLevel


@dataclass(frozen=True, slots=True)
class VadEvent:
    """一次 VAD 状态迁移：说话开始 / 结束。"""

    kind: str  # "utterance_started" | "utterance_ended"


class VoiceActivityDetector(Protocol):
    """喂入 PCM16 单声道帧，输出断句事件。"""

    def feed(self, pcm: bytes) -> VadEvent | None: ...


class SpeechRecognizer(Protocol):
    """整段话语转写。runs_local=False 的实现禁止接收 L2 音频。"""

    runs_local: bool

    async def transcribe(
        self, pcm: bytes, *, sample_rate: int, language: str | None
    ) -> str: ...


class SpeechSynthesizer(Protocol):
    """句级流式合成：产出 PCM16 音频分片（mime / sample_rate 由实现方声明）。"""

    runs_local: bool
    mime: str
    sample_rate: int

    def synthesize(
        self, text: str, *, privacy_level: PrivacyLevel
    ) -> AsyncIterator[bytes]: ...


class LocalOnlySynthesizerError(PermissionError):
    """该隐私等级没有可用的本地 TTS，语音输出必须降级为文字。"""

    def __init__(self, privacy_level: PrivacyLevel) -> None:
        self.privacy_level = privacy_level
        super().__init__(f"no local tts available for {privacy_level.value}")
