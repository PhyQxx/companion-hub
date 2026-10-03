"""P6 语音化（docs/04）：语音管线核心组件与导出。"""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from app.voice.contracts import (
        LocalOnlySynthesizerError,
        SpeechRecognitionUnavailable,
        SpeechRecognizer,
        SpeechSynthesizer,
        StreamingSpeechRecognizer,
        VadEvent,
        VoiceActivityDetector,
        WakeWordDetector,
        WakeWordUnavailable,
    )
    from app.voice.factory import (
        ConfigVoiceSource,
        StaticVoiceSource,
        VoiceProviderSource,
        build_voice_providers,
    )
    from app.voice.failover import TtsProviderChain, TtsSelection
    from app.voice.faster_whisper import FasterWhisperRecognizer
    from app.voice.metrics import VoiceLatencyMetrics, VoiceLatencySample
    from app.voice.mimo import (
        ASR_MODEL,
        DEFAULT_TTS_VOICE,
        TTS_MODEL,
        MiMoAsrRecognizer,
        MiMoTtsSynthesizer,
        wrap_wav,
    )
    from app.voice.pipeline import PcmAmplitudeEnvelope, SentenceBuffer
    from app.voice.speech_text import MarkdownSpeechFilter, markdown_to_speech_text
    from app.voice.tts import EdgeTtsSynthesizer
    from app.voice.vad import (
        EnergyVad,
        ResilientVad,
        SileroVad,
        create_default_vad,
        pcm16_rms,
    )
    from app.voice.wakeword import OpenWakeWordDetector, create_default_wake_word

_EXPORTS = {
    "ASR_MODEL": ("app.voice.mimo", "ASR_MODEL"),
    "DEFAULT_TTS_VOICE": ("app.voice.mimo", "DEFAULT_TTS_VOICE"),
    "TTS_MODEL": ("app.voice.mimo", "TTS_MODEL"),
    "ConfigVoiceSource": ("app.voice.factory", "ConfigVoiceSource"),
    "EdgeTtsSynthesizer": ("app.voice.tts", "EdgeTtsSynthesizer"),
    "EnergyVad": ("app.voice.vad", "EnergyVad"),
    "FasterWhisperRecognizer": ("app.voice.faster_whisper", "FasterWhisperRecognizer"),
    "LocalOnlySynthesizerError": ("app.voice.contracts", "LocalOnlySynthesizerError"),
    "MarkdownSpeechFilter": ("app.voice.speech_text", "MarkdownSpeechFilter"),
    "MiMoAsrRecognizer": ("app.voice.mimo", "MiMoAsrRecognizer"),
    "MiMoTtsSynthesizer": ("app.voice.mimo", "MiMoTtsSynthesizer"),
    "OpenWakeWordDetector": ("app.voice.wakeword", "OpenWakeWordDetector"),
    "PcmAmplitudeEnvelope": ("app.voice.pipeline", "PcmAmplitudeEnvelope"),
    "ResilientVad": ("app.voice.vad", "ResilientVad"),
    "SentenceBuffer": ("app.voice.pipeline", "SentenceBuffer"),
    "SileroVad": ("app.voice.vad", "SileroVad"),
    "SpeechRecognitionUnavailable": ("app.voice.contracts", "SpeechRecognitionUnavailable"),
    "SpeechRecognizer": ("app.voice.contracts", "SpeechRecognizer"),
    "SpeechSynthesizer": ("app.voice.contracts", "SpeechSynthesizer"),
    "StaticVoiceSource": ("app.voice.factory", "StaticVoiceSource"),
    "StreamingSpeechRecognizer": ("app.voice.contracts", "StreamingSpeechRecognizer"),
    "TtsProviderChain": ("app.voice.failover", "TtsProviderChain"),
    "TtsSelection": ("app.voice.failover", "TtsSelection"),
    "VadEvent": ("app.voice.contracts", "VadEvent"),
    "VoiceActivityDetector": ("app.voice.contracts", "VoiceActivityDetector"),
    "VoiceLatencyMetrics": ("app.voice.metrics", "VoiceLatencyMetrics"),
    "VoiceLatencySample": ("app.voice.metrics", "VoiceLatencySample"),
    "VoiceProviderSource": ("app.voice.factory", "VoiceProviderSource"),
    "WakeWordDetector": ("app.voice.contracts", "WakeWordDetector"),
    "WakeWordUnavailable": ("app.voice.contracts", "WakeWordUnavailable"),
    "build_voice_providers": ("app.voice.factory", "build_voice_providers"),
    "create_default_vad": ("app.voice.vad", "create_default_vad"),
    "create_default_wake_word": ("app.voice.wakeword", "create_default_wake_word"),
    "markdown_to_speech_text": ("app.voice.speech_text", "markdown_to_speech_text"),
    "pcm16_rms": ("app.voice.vad", "pcm16_rms"),
    "wrap_wav": ("app.voice.mimo", "wrap_wav"),
}

__all__ = [
    "ASR_MODEL",
    "DEFAULT_TTS_VOICE",
    "TTS_MODEL",
    "ConfigVoiceSource",
    "EdgeTtsSynthesizer",
    "EnergyVad",
    "FasterWhisperRecognizer",
    "LocalOnlySynthesizerError",
    "MarkdownSpeechFilter",
    "MiMoAsrRecognizer",
    "MiMoTtsSynthesizer",
    "OpenWakeWordDetector",
    "PcmAmplitudeEnvelope",
    "ResilientVad",
    "SentenceBuffer",
    "SileroVad",
    "SpeechRecognitionUnavailable",
    "SpeechRecognizer",
    "SpeechSynthesizer",
    "StaticVoiceSource",
    "StreamingSpeechRecognizer",
    "TtsProviderChain",
    "TtsSelection",
    "VadEvent",
    "VoiceActivityDetector",
    "VoiceLatencyMetrics",
    "VoiceLatencySample",
    "VoiceProviderSource",
    "WakeWordDetector",
    "WakeWordUnavailable",
    "build_voice_providers",
    "create_default_vad",
    "create_default_wake_word",
    "markdown_to_speech_text",
    "pcm16_rms",
    "wrap_wav",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
