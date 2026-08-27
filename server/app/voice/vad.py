# ruff: noqa: RUF003
"""语音活动检测：优先 Silero，运行条件不足时稳定回退到能量 VAD。

自动断句使用统一 ``VoiceActivityDetector`` 契约，因此 ``/ws/voice``、PTT 与
barge-in 不关心具体实现。Silero 只负责自动断句概率判定；播放中打断继续使用
轻量 PCM16 RMS，避免额外调用有状态 Silero 模型扰乱流式隐状态。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from collections.abc import Callable
from math import isqrt
from typing import Any

from app.voice.contracts import VadEvent, VoiceActivityDetector

logger = logging.getLogger(__name__)

ENERGY_FRAME_MS = 30
ENERGY_BYTES_PER_FRAME = 960  # PCM16 单声道 16kHz × 30ms
SILERO_SAMPLE_RATE = 16_000
SILERO_SAMPLES_PER_FRAME = 512
SILERO_BYTES_PER_FRAME = SILERO_SAMPLES_PER_FRAME * 2  # 32ms
SILERO_FRAME_MS = 32


def pcm16_rms(pcm: bytes) -> int:
    """计算 little-endian PCM16 的整数 RMS；尾部孤立字节安全忽略。"""
    sample_bytes = len(pcm) - (len(pcm) % 2)
    if sample_bytes == 0:
        return 0
    total = 0
    view = memoryview(pcm)
    for offset in range(0, sample_bytes, 2):
        sample = int.from_bytes(view[offset : offset + 2], "little", signed=True)
        total += sample * sample
    return isqrt(total // (sample_bytes // 2))


class EnergyVad:
    """帧级 RMS 状态机：连续 N 帧过阈判开始，连续 M 帧低于阈值判结束。"""

    def __init__(
        self,
        *,
        threshold_rms: int = 550,
        start_frames: int = 3,
        end_frames: int = 15,
        max_utterance_ms: int = 30_000,
    ) -> None:
        self._threshold = threshold_rms
        self._start_frames = start_frames
        self._end_frames = end_frames
        self._max_frames = max(1, max_utterance_ms // ENERGY_FRAME_MS)
        self._buffer = bytearray()
        self._speaking = False
        self._voice_run = 0
        self._silence_run = 0
        self._frames_in_utterance = 0

    @property
    def backend(self) -> str:
        return "energy"

    @property
    def speaking(self) -> bool:
        return self._speaking

    def feed(self, pcm: bytes) -> VadEvent | None:
        """喂入任意长度 PCM；按内部 30ms 帧推进，返回最近一次状态迁移。"""
        self._buffer.extend(pcm)
        event: VadEvent | None = None
        while len(self._buffer) >= ENERGY_BYTES_PER_FRAME:
            frame = bytes(self._buffer[:ENERGY_BYTES_PER_FRAME])
            del self._buffer[:ENERGY_BYTES_PER_FRAME]
            step = self._step(frame)
            if step is not None:
                event = step
        return event

    def force_end(self) -> VadEvent | None:
        """外部强制断句（PTT 松开 / 会话关闭）。"""
        if not self._speaking:
            self._reset()
            return None
        self._reset()
        return VadEvent("utterance_ended")

    def is_voiced(self, pcm: bytes) -> bool:
        """打断检测用轻量能量门，不影响自动断句状态。"""
        return pcm16_rms(pcm) >= self._threshold

    def _step(self, frame: bytes) -> VadEvent | None:
        voiced = pcm16_rms(frame) >= self._threshold
        if not self._speaking:
            self._voice_run = self._voice_run + 1 if voiced else 0
            if self._voice_run >= self._start_frames:
                self._speaking = True
                self._frames_in_utterance = self._voice_run
                self._silence_run = 0
                return VadEvent("utterance_started")
            return None
        self._frames_in_utterance += 1
        self._silence_run = 0 if voiced else self._silence_run + 1
        if self._silence_run >= self._end_frames or (
            self._frames_in_utterance >= self._max_frames
        ):
            self._reset()
            return VadEvent("utterance_ended")
        return None

    def _reset(self) -> None:
        self._buffer.clear()
        self._speaking = False
        self._voice_run = 0
        self._silence_run = 0
        self._frames_in_utterance = 0


class SileroVadUnavailable(RuntimeError):
    """Silero Python 依赖或模型运行时不可用。"""


class _SileroProbabilityModel:
    """延迟加载 ``silero-vad``，避免 Hub 启动时强制引入本地 ML 运行时。"""

    def __init__(self) -> None:
        self._model: Any | None = None
        self._torch: Any | None = None

    def __call__(self, pcm: bytes) -> float:
        self._ensure_loaded()
        if self._model is None or self._torch is None:
            raise SileroVadUnavailable("silero_model_not_loaded")
        samples = _pcm16_to_float(pcm)
        tensor = self._torch.tensor(samples, dtype=self._torch.float32)
        result = self._model(tensor, SILERO_SAMPLE_RATE)
        item = getattr(result, "item", None)
        return float(item() if callable(item) else result)

    def reset(self) -> None:
        if self._model is None:
            return
        reset = getattr(self._model, "reset_states", None)
        if callable(reset):
            reset()

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            silero_vad = importlib.import_module("silero_vad")
            torch = importlib.import_module("torch")
            load = silero_vad.load_silero_vad
            self._model = load(onnx=True)
            self._torch = torch
        except Exception as error:
            raise SileroVadUnavailable(type(error).__name__) from error


class SileroVad:
    """Silero speech probability + 本项目稳定起止/hangover 状态机。"""

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        start_frames: int = 2,
        end_frames: int = 14,
        max_utterance_ms: int = 30_000,
        barge_in_threshold_rms: int = 550,
        probability_fn: Callable[[bytes], float] | None = None,
        reset_fn: Callable[[], None] | None = None,
    ) -> None:
        self._threshold = threshold
        self._start_frames = start_frames
        self._end_frames = end_frames
        self._max_frames = max(1, max_utterance_ms // SILERO_FRAME_MS)
        self._barge_in_threshold = barge_in_threshold_rms
        self._buffer = bytearray()
        self._speaking = False
        self._voice_run = 0
        self._silence_run = 0
        self._frames_in_utterance = 0
        self._runtime = _SileroProbabilityModel() if probability_fn is None else None
        self._probability_fn = probability_fn or self._runtime_probability
        self._reset_fn = reset_fn or self._runtime_reset

    @property
    def backend(self) -> str:
        return "silero"

    @property
    def speaking(self) -> bool:
        return self._speaking

    def feed(self, pcm: bytes) -> VadEvent | None:
        self._buffer.extend(pcm)
        event: VadEvent | None = None
        while len(self._buffer) >= SILERO_BYTES_PER_FRAME:
            frame = bytes(self._buffer[:SILERO_BYTES_PER_FRAME])
            del self._buffer[:SILERO_BYTES_PER_FRAME]
            step = self._step(frame)
            if step is not None:
                event = step
        return event

    def force_end(self) -> VadEvent | None:
        was_speaking = self._speaking
        self._reset()
        return VadEvent("utterance_ended") if was_speaking else None

    def is_voiced(self, pcm: bytes) -> bool:
        # Silero 模型是有状态的；barge-in 独立走 RMS，避免一次探测推进隐状态。
        return pcm16_rms(pcm) >= self._barge_in_threshold

    def _step(self, frame: bytes) -> VadEvent | None:
        voiced = self._probability_fn(frame) >= self._threshold
        if not self._speaking:
            self._voice_run = self._voice_run + 1 if voiced else 0
            if self._voice_run >= self._start_frames:
                self._speaking = True
                self._frames_in_utterance = self._voice_run
                self._silence_run = 0
                return VadEvent("utterance_started")
            return None
        self._frames_in_utterance += 1
        self._silence_run = 0 if voiced else self._silence_run + 1
        if self._silence_run >= self._end_frames or (
            self._frames_in_utterance >= self._max_frames
        ):
            self._reset()
            return VadEvent("utterance_ended")
        return None

    def _runtime_probability(self, pcm: bytes) -> float:
        if self._runtime is None:
            raise SileroVadUnavailable("silero_runtime_missing")
        return self._runtime(pcm)

    def _runtime_reset(self) -> None:
        if self._runtime is not None:
            self._runtime.reset()

    def _reset(self) -> None:
        self._buffer.clear()
        self._speaking = False
        self._voice_run = 0
        self._silence_run = 0
        self._frames_in_utterance = 0
        self._reset_fn()


class ResilientVad:
    """主 VAD 运行失败时一次性切到能量 VAD，不中断语音会话。"""

    def __init__(self, primary: VoiceActivityDetector, fallback: VoiceActivityDetector) -> None:
        self._active = primary
        self._fallback = fallback
        self._degraded = False

    @property
    def backend(self) -> str:
        return self._active.backend

    @property
    def speaking(self) -> bool:
        return self._active.speaking

    def feed(self, pcm: bytes) -> VadEvent | None:
        try:
            return self._active.feed(pcm)
        except Exception as error:
            if self._degraded:
                raise
            logger.warning(
                "silero vad unavailable; falling back to energy vad error_type=%s",
                type(error).__name__,
            )
            self._degraded = True
            self._active = self._fallback
            return self._active.feed(pcm)

    def force_end(self) -> VadEvent | None:
        return self._active.force_end()

    def is_voiced(self, pcm: bytes) -> bool:
        return self._active.is_voiced(pcm)


def create_default_vad() -> VoiceActivityDetector:
    """有 Silero 依赖则优先使用，否则保持无依赖能量 VAD。"""
    if importlib.util.find_spec("silero_vad") is None or importlib.util.find_spec("torch") is None:
        return EnergyVad()
    return ResilientVad(SileroVad(), EnergyVad())


def _pcm16_to_float(pcm: bytes) -> list[float]:
    view = memoryview(pcm)
    return [
        int.from_bytes(view[offset : offset + 2], "little", signed=True) / 32_768.0
        for offset in range(0, len(pcm) - (len(pcm) % 2), 2)
    ]
