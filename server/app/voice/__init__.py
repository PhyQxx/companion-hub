"""P6 语音化（docs/04）：语音管线核心组件与导出。"""

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
