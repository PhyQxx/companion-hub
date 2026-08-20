# ruff: noqa: RUF002, RUF003
"""能量 VAD：PCM16 RMS 阈值 + hangover 尾帧的断句状态机。

silero-vad（M2 后续批次）落地前保留确定性能量断句；RMS 在本模块
直接计算，避免依赖 Python 3.13 已移除的 ``audioop``。接口继续按
VadEvent 状态迁移设计，后续替换实现不需要改动调用侧。
"""

from __future__ import annotations

from math import isqrt

from app.voice.contracts import VadEvent

FRAME_MS = 30
BYTES_PER_FRAME = 960  # PCM16 单声道 16kHz × 30ms


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
        self._max_frames = max(1, max_utterance_ms // FRAME_MS)
        self._buffer = bytearray()
        self._speaking = False
        self._voice_run = 0
        self._silence_run = 0
        self._frames_in_utterance = 0

    @property
    def speaking(self) -> bool:
        return self._speaking

    def feed(self, pcm: bytes) -> VadEvent | None:
        """喂入任意长度 PCM；按内部 30ms 帧推进，返回最近一次状态迁移。"""
        self._buffer.extend(pcm)
        event: VadEvent | None = None
        while len(self._buffer) >= BYTES_PER_FRAME:
            frame = bytes(self._buffer[:BYTES_PER_FRAME])
            del self._buffer[:BYTES_PER_FRAME]
            step = self._step(frame)
            if step is not None:
                event = step
        return event

    def force_end(self) -> VadEvent | None:
        """外部强制断句（PTT 松开 / 会话关闭）。"""
        if not self._speaking:
            return None
        self._reset()
        return VadEvent("utterance_ended")

    def is_voiced(self, pcm: bytes) -> bool:
        """单帧人声判定：打断检测用，灵敏度高于断句（无 hangover）。"""
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
        # 超长话语强制断句，避免 ASR 缓冲无界增长
        if self._silence_run >= self._end_frames or (
            self._frames_in_utterance >= self._max_frames
        ):
            self._reset()
            return VadEvent("utterance_ended")
        return None

    def _reset(self) -> None:
        self._speaking = False
        self._voice_run = 0
        self._silence_run = 0
        self._frames_in_utterance = 0
