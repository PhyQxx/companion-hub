from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.schemas import EphemeralSignal


@dataclass(frozen=True, slots=True)
class BufferMetrics:
    accepted: int
    expired: int
    dropped: int
    depth: int


class EphemeralBuffer:
    """Bounded, process-local buffer. It deliberately exposes no persistence hook."""

    def __init__(
        self,
        maxsize: int = 256,
        *,
        overflow: Literal["block", "drop_oldest"] = "drop_oldest",
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be positive")
        self._queue: asyncio.Queue[EphemeralSignal] = asyncio.Queue(maxsize=maxsize)
        self._overflow = overflow
        self._accepted = 0
        self._expired = 0
        self._dropped = 0

    async def put(self, signal: EphemeralSignal, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        if signal.expires_at <= current:
            self._expired += 1
            return False
        if self._overflow == "drop_oldest" and self._queue.full():
            self._queue.get_nowait()
            self._queue.task_done()
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
            depth=self._queue.qsize(),
        )
