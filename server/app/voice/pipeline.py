# ruff: noqa: RUF001
"""句级切分：LLM 流式输出按强标点切句，首句立即送 TTS（docs/02 §3.2）。"""

from __future__ import annotations

from app.voice.vad import pcm16_rms

SENTENCE_TERMINATORS = ("。", "！", "？", "!", "?", "\n")
MAX_SENTENCE_CHARS = 60
VISEME_WINDOW_MS = 50


class SentenceBuffer:
    """累积流式 delta，遇到句末标点或达到长度上限时产出完整句子。"""

    def __init__(self, *, max_chars: int = MAX_SENTENCE_CHARS) -> None:
        self._pending = ""
        self._max_chars = max_chars

    def push(self, delta: str) -> list[str]:
        self._pending += delta
        sentences: list[str] = []
        while True:
            cut = min(
                (index for index in map(self._pending.find, SENTENCE_TERMINATORS) if index >= 0),
                default=-1,
            )
            if cut < 0:
                break
            sentence = self._pending[: cut + 1].strip()
            self._pending = self._pending[cut + 1 :]
            if sentence:
                sentences.append(sentence)
        while len(self._pending) > self._max_chars:
            sentences.append(self._pending[: self._max_chars])
            self._pending = self._pending[self._max_chars :]
        return sentences

    def flush(self) -> str:
        """回合结束时取走残余文本（无标点收尾的尾巴句）。"""
        remainder = self._pending.strip()
        self._pending = ""
        return remainder


class PcmAmplitudeEnvelope:
    """把 PCM16 音频按固定窗口转换为 0..1 嘴部开合幅度。"""

    def __init__(self, sample_rate: int, *, window_ms: int = VISEME_WINDOW_MS) -> None:
        if sample_rate <= 0 or window_ms <= 0:
            raise ValueError("sample_rate and window_ms must be positive")
        self.window_ms = window_ms
        self._bytes_per_window = max(2, sample_rate * 2 * window_ms // 1000)
        self._buffer = bytearray()

    def push(self, pcm: bytes) -> list[float]:
        self._buffer.extend(pcm)
        amplitudes: list[float] = []
        while len(self._buffer) >= self._bytes_per_window:
            frame = bytes(self._buffer[: self._bytes_per_window])
            del self._buffer[: self._bytes_per_window]
            amplitudes.append(min(1.0, pcm16_rms(frame) / 32_768.0))
        return amplitudes
