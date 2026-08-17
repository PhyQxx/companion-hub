from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .dispatcher import DispatchResult, EventPublisher, dispatch_once


@dataclass(slots=True)
class WorkerState:
    running: bool = False
    cycles: int = 0
    dispatched: int = 0
    retried: int = 0
    dead: int = 0
    last_error: str | None = None


class DispatcherWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        publisher: EventPublisher,
        *,
        poll_seconds: float = 0.25,
        batch_size: int = 50,
        max_attempts: int = 8,
        owner: str | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._sessions = sessions
        self._publisher = publisher
        self._poll_seconds = poll_seconds
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._owner = owner or f"hub-{uuid4()}"
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.state = WorkerState()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="aria-outbox-dispatcher")
        await asyncio.sleep(0)

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def run(self) -> None:
        self.state.running = True
        try:
            while not self._stop.is_set():
                try:
                    result = await dispatch_once(
                        self._sessions,
                        self._publisher,
                        owner=self._owner,
                        batch_size=self._batch_size,
                        max_attempts=self._max_attempts,
                    )
                    self._record(result)
                    self.state.last_error = None
                except Exception as error:
                    self.state.last_error = type(error).__name__
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
        finally:
            self.state.running = False

    def _record(self, result: DispatchResult) -> None:
        self.state.cycles += 1
        self.state.dispatched += result.dispatched
        self.state.retried += result.retried
        self.state.dead += result.dead
