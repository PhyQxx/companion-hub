"""Durable, source-referenced chat maintenance on the existing JobEngine."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import update

from app.db import Database, JobRecord
from app.ids import uuid7
from app.jobs import JobEngine

logger = logging.getLogger(__name__)
RESOURCE = "chat-postcommit"
Handler = Callable[[str, dict[str, Any], str], Awaitable[None]]


class PostcommitSourceGone(RuntimeError):
    pass


class PostcommitWorker:
    def __init__(self, database: Database, handler: Handler) -> None:
        self.engine = JobEngine(database)
        self._database = database
        self._handler = handler
        self._worker_id = f"chat-{uuid7()}"
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="chat-postcommit-worker")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def drain_ready(self) -> None:
        self._stop.clear()
        await self.process_ready()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_ready()
            except Exception:
                logger.exception("postcommit worker cycle failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), 2)

    async def process_ready(self) -> None:
        async with self._lock:
            # Only this maintenance class is recovered; unrelated jobs keep
            # their existing scheduling and cancellation semantics.
            async with self._database.sessions.begin() as session:
                await session.execute(
                    update(JobRecord)
                    .where(
                        JobRecord.resource_class == RESOURCE,
                        JobRecord.status == "retry_wait",
                        JobRecord.available_at <= datetime.now(UTC),
                    )
                    .values(status="queued")
                )
            await self.engine.expire_stale_leases(resource_class=RESOURCE)
            while not self._stop.is_set():
                job = await self.engine.claim(
                    self._worker_id, resource_class=RESOURCE, lease_seconds=300
                )
                if job is None:
                    return
                step_id = await self.engine.start_step(
                    job.id,
                    "derive",
                    worker_id=self._worker_id,
                    claim_version=job.attempts,
                )
                heartbeat = asyncio.create_task(self._renew(job.id))
                try:
                    if await self.engine.cancel_requested(job.id):
                        raise PostcommitSourceGone()
                    payload = await self.engine.job_input(job.id)
                    if payload is None:
                        raise PostcommitSourceGone()
                    await self._handler(job.kind, payload, job.owner)
                    await self.engine.complete_step(step_id)
                    await self.engine.succeed(
                        job.id, worker_id=self._worker_id, claim_version=job.attempts
                    )
                except PostcommitSourceGone:
                    await self.engine.cancel(
                        job.id, worker_id=self._worker_id, claim_version=job.attempts
                    )
                    await self.engine.confirm_cancelled(job.id, self._worker_id)
                except Exception:
                    logger.warning("postcommit failed job=%s kind=%s", job.id, job.kind)
                    if await self.engine.cancel_requested(job.id):
                        await self.engine.confirm_cancelled(job.id, self._worker_id)
                    else:
                        await self.engine.fail_step(step_id, error_code="postcommit_failed")
                finally:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat

    async def _renew(self, job_id: UUID) -> None:
        while True:
            await asyncio.sleep(30)
            if not await self.engine.renew_lease(job_id, self._worker_id, lease_seconds=300):
                return
