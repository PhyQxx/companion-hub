from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.schemas import EphemeralSignal
from app.schemas.adapter import InputPolicy


@dataclass(frozen=True, slots=True)
class BufferMetrics:
    accepted: int
    expired: int
    dropped: int
    aggregated: int
    depth: int


class EphemeralBuffer:
    """Bounded, process-local buffer. It deliberately exposes no persistence hook."""

    def __init__(
        self,
        maxsize: int = 256,
        *,
        overflow: Literal["block", "drop_oldest", "sample", "aggregate"] = "drop_oldest",
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be positive")
        self._queue: asyncio.Queue[EphemeralSignal] = asyncio.Queue(maxsize=maxsize)
        self._overflow = overflow
        self._accepted = 0
        self._expired = 0
        self._dropped = 0
        self._aggregated = 0

    @classmethod
    def from_policy(cls, policy: InputPolicy) -> EphemeralBuffer:
        return cls(maxsize=policy.queue_limit, overflow=policy.backpressure)

    async def put(self, signal: EphemeralSignal, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        if signal.expires_at <= current:
            self._expired += 1
            return False
        if self._queue.full():
            if self._overflow == "sample":
                self._dropped += 1
                return False
            if self._overflow in {"drop_oldest", "aggregate"}:
                previous = self._queue.get_nowait()
                self._queue.task_done()
                if self._overflow == "aggregate":
                    signal, combined = self._aggregate(previous, signal)
                    if combined:
                        self._aggregated += 1
                    else:
                        self._dropped += 1
                else:
                    self._dropped += 1
        await self._queue.put(signal)
        self._accepted += 1
        return True

    async def get(self, *, now: datetime | None = None) -> EphemeralSignal:
        while True:
            signal = await self._queue.get()
            self._queue.task_done()
            current = now or datetime.now(UTC)
            if signal.expires_at > current:
                return signal
            self._expired += 1

    @property
    def metrics(self) -> BufferMetrics:
        return BufferMetrics(
            accepted=self._accepted,
            expired=self._expired,
            dropped=self._dropped,
            aggregated=self._aggregated,
            depth=self._queue.qsize(),
        )

    @staticmethod
    def _aggregate(
        previous: EphemeralSignal, current: EphemeralSignal
    ) -> tuple[EphemeralSignal, bool]:
        previous_content = previous.content
        current_content = current.content
        if (
            previous_content.type == "telemetry"
            and current_content.type == "telemetry"
            and previous_content.channel == current_content.channel
            and isinstance(previous_content.value, (int, float))
            and not isinstance(previous_content.value, bool)
            and isinstance(current_content.value, (int, float))
            and not isinstance(current_content.value, bool)
        ):
            content = current_content.model_dump(mode="python")
            content["value"] = (previous_content.value + current_content.value) / 2
            return (
                EphemeralSignal.model_validate(
                    {**current.model_dump(mode="python"), "content": content}
                ),
                True,
            )
        return current, False
