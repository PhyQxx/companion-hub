# ruff: noqa: RUF002
"""openWakeWord 可选唤醒门：运行时存在才启用，不阻塞既有 VAD/PTT。"""

from __future__ import annotations

import importlib
import importlib.util
import os
from collections.abc import Callable
from typing import Any

from app.voice.contracts import WakeWordDetector, WakeWordUnavailable

SAMPLE_RATE = 16_000
SAMPLES_PER_FRAME = 1_280  # openWakeWord 推荐约 80ms / 16kHz PCM16
BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2


class OpenWakeWordDetector:
    """openWakeWord 分数门；predictor 注入用于确定性测试。"""

    def __init__(
        self,
        *,
        model_name: str = "hey_jarvis",
        threshold: float = 0.5,
        predictor: Callable[[bytes], float] | None = None,
        reset_fn: Callable[[], None] | None = None,
    ) -> None:
        self._model_name = model_name
        self._threshold = threshold
        self._buffer = bytearray()
        self._runtime: _OpenWakeWordRuntime | None = None
        self._predictor = predictor or self._runtime_predict
        self._reset_fn = reset_fn or self._runtime_reset

    @property
    def backend(self) -> str:
        return "openwakeword"

    def feed(self, pcm: bytes) -> bool:
        self._buffer.extend(pcm)
        while len(self._buffer) >= BYTES_PER_FRAME:
            frame = bytes(self._buffer[:BYTES_PER_FRAME])
            del self._buffer[:BYTES_PER_FRAME]
            if self._predictor(frame) >= self._threshold:
                self.reset()
                return True
        return False

    def reset(self) -> None:
        self._buffer.clear()
        self._reset_fn()

    def _runtime_predict(self, pcm: bytes) -> float:
        if self._runtime is None:
            self._runtime = _OpenWakeWordRuntime(self._model_name)
        return self._runtime(pcm)

    def _runtime_reset(self) -> None:
        if self._runtime is not None:
            self._runtime.reset()


class _OpenWakeWordRuntime:
    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model: Any | None = None
        self._numpy: Any | None = None

    def __call__(self, pcm: bytes) -> float:
        self._ensure_loaded()
        if self._model is None or self._numpy is None:
            raise WakeWordUnavailable("openwakeword_model_not_loaded")
        samples = self._numpy.frombuffer(pcm, dtype=self._numpy.int16)
        scores = self._model.predict(samples)
        if not isinstance(scores, dict):
            raise WakeWordUnavailable("openwakeword_invalid_result")
        score = scores.get(self._model_name)
        if score is None and len(scores) == 1:
            score = next(iter(scores.values()))
        try:
            return float(score or 0.0)
        except (TypeError, ValueError) as error:
            raise WakeWordUnavailable("openwakeword_invalid_score") from error

    def reset(self) -> None:
        if self._model is None:
            return
        reset = getattr(self._model, "reset", None)
        if callable(reset):
            reset()

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            model_module = importlib.import_module("openwakeword.model")
            self._numpy = importlib.import_module("numpy")
            self._model = model_module.Model(wakeword_models=[self._model_name])
        except Exception as error:
            raise WakeWordUnavailable(type(error).__name__) from error


def create_default_wake_word() -> WakeWordDetector | None:
    """依赖存在时启用 openWakeWord，否则保持原有自动 VAD/PTT 行为。"""
    if (
        importlib.util.find_spec("openwakeword") is None
        or importlib.util.find_spec("numpy") is None
    ):
        return None
    model_name = os.getenv("ARIA_WAKE_WORD_MODEL", "hey_jarvis").strip() or "hey_jarvis"
    return OpenWakeWordDetector(model_name=model_name)
