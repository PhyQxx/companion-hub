"""语音延迟滑动窗口统计：为 M2 P50/P90 验收提供轻量运行时报告。"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VoiceLatencySample:
    asr_ms: int | None
    first_token_ms: int | None
    first_audio_ms: int | None
    total_ms: int


class VoiceLatencyMetrics:
    def __init__(self, *, max_samples: int = 200) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        self._samples: deque[VoiceLatencySample] = deque(maxlen=max_samples)
        self._interrupt_ms: deque[int] = deque(maxlen=max_samples)

    def record(self, sample: VoiceLatencySample) -> None:
        self._samples.append(sample)

    def record_interrupt(self, elapsed_ms: int) -> None:
        self._interrupt_ms.append(max(0, elapsed_ms))

    def clear(self) -> None:
        self._samples.clear()
        self._interrupt_ms.clear()

    def snapshot(self) -> dict[str, object]:
        samples = list(self._samples)
        first_audio = _summary(sample.first_audio_ms for sample in samples)
        interrupt = _summary(self._interrupt_ms)
        first_audio_p90 = first_audio["p90"]
        interrupt_p90 = interrupt["p90"]
        interrupt_count = interrupt["count"]
        completed_enough = len(samples) >= 20
        first_audio_count = first_audio["count"]
        first_audio_enough = (
            isinstance(first_audio_count, int) and first_audio_count >= 20
        )
        interrupts_enough = isinstance(interrupt_count, int) and interrupt_count >= 20
        first_audio_pass = (
            first_audio_enough
            and isinstance(first_audio_p90, int)
            and first_audio_p90 <= 1800
        )
        interrupt_pass = (
            interrupts_enough
            and isinstance(interrupt_p90, int)
            and interrupt_p90 <= 300
        )
        return {
            "count": len(samples),
            "window_size": self._samples.maxlen,
            "asr_ms": _summary(sample.asr_ms for sample in samples),
            "first_token_ms": _summary(sample.first_token_ms for sample in samples),
            "first_audio_ms": first_audio,
            "total_ms": _summary(sample.total_ms for sample in samples),
            "interrupt_ms": interrupt,
            "targets": {
                "completed_turns": 20,
                "first_audio_samples": 20,
                "interrupt_samples": 20,
                "first_audio_p90_ms": 1800,
                "interrupt_p90_ms": 300,
            },
            "acceptance": {
                "completed_turns_ready": completed_enough,
                "first_audio_samples_ready": first_audio_enough,
                "interrupt_samples_ready": interrupts_enough,
                "first_audio_p90_pass": first_audio_pass,
                "interrupt_p90_pass": interrupt_pass,
                "overall_pass": completed_enough
                and first_audio_pass
                and interrupt_pass,
            },
        }


def _summary(values: Iterable[int | None]) -> dict[str, int | None]:
    filtered = sorted(value for value in values if isinstance(value, int))
    if not filtered:
        return {"count": 0, "p50": None, "p90": None, "max": None}
    return {
        "count": len(filtered),
        "p50": _percentile(filtered, 0.50),
        "p90": _percentile(filtered, 0.90),
        "max": filtered[-1],
    }


def _percentile(values: list[int], quantile: float) -> int:
    index = max(0, min(len(values) - 1, int((len(values) - 1) * quantile + 0.5)))
    return values[index]
