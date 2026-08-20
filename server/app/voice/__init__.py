# ruff: noqa: RUF002
"""P6 语音化（docs/33）：语音管线核心组件与导出。"""

from app.voice.contracts import (
    LocalOnlySynthesizerError,
    SpeechRecognizer,
    SpeechSynthesizer,
    VadEvent,
    VoiceActivityDetector,
)
from app.voice.failover import TtsProviderChain, TtsSelection
from app.voice.mimo import (
    ASR_MODEL,
    DEFAULT_TTS_VOICE,
    TTS_MODEL,
    MiMoAsrRecognizer,
    MiMoTtsSynthesizer,
    wrap_wav,
)
from app.voice.pipeline import SentenceBuffer
from app.voice.tts import EdgeTtsSynthesizer
from app.voice.vad import EnergyVad

__all__ = [
    "ASR_MODEL",
    "DEFAULT_TTS_VOICE",
    "TTS_MODEL",
    "EdgeTtsSynthesizer",
    "EnergyVad",
    "LocalOnlySynthesizerError",
    "MiMoAsrRecognizer",
    "MiMoTtsSynthesizer",
    "SentenceBuffer",
    "SpeechRecognizer",
    "SpeechSynthesizer",
    "TtsProviderChain",
    "TtsSelection",
    "VadEvent",
    "VoiceActivityDetector",
    "wrap_wav",
]
