"""Owned durable maintenance on the existing JobEngine, with request-local claim context."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from app.db import Database
from app.harness.budget import BudgetDenied
from app.harness.claim import ClaimInvalidated, ExecutionClaim, claim_scope
from app.ids import uuid7
from app.jobs.engine import JobEngine, JobView

logger = logging.getLogger(__name__)
_CURRENT_JOB: ContextVar[JobView | None] = ContextVar("maintenance_job", default=None)


def maintenance_job() -> JobView:
    job = _CURRENT_JOB.get()
    if job is None:
        raise RuntimeError("maintenance_claim_missing")
    return job


Handler = Callable[[str, dict[str, Any], str], Awaitable[None]]


MaintenanceSourceGone = ClaimInvalidated


class MaintenanceWorker:
    def __init__(
        self,
        database: Database,
        handler: Handler,
        *,
        resource_class: str,
        error_code: str = "maintenance_failed",
    ) -> None:
        self.engine = JobEngine(database)
        self._database = database
        self._resource = resource_class
        self._error_code = error_code
        self._handler = handler
        self._worker_id = f"{resource_class}-{uuid7()}"
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name=f"{self._resource}-worker")

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
                logger.exception("maintenance worker cycle failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), 2)

    async def process_ready(self) -> None:
        async with self._lock:
            # Only this maintenance class is recovered; unrelated jobs keep
            # their existing scheduling and cancellation semantics.
            await self.engine.release_ready_retries(resource_class=self._resource)
            await self.engine.expire_stale_leases(resource_class=self._resource)
            while not self._stop.is_set():
                job = await self.engine.claim(
                    self._worker_id, resource_class=self._resource, lease_seconds=300
                )
                if job is None:
                    return
                step_id = await self.engine.start_step(
                    job.id,
                    "derive",
                    worker_id=self._worker_id,
                    claim_version=job.attempts,
                )
                heartbeat = asyncio.create_task(self._renew(job.id, job.attempts))
                context_token = _CURRENT_JOB.set(job)
                try:
                    if await self.engine.cancel_requested(job.id):
                        raise MaintenanceSourceGone()
                    payload = await self.engine.job_input(job.id)
                    if payload is None:
                        raise MaintenanceSourceGone()
                    with claim_scope(ExecutionClaim(job.id, self._worker_id, job.attempts)):
                        await self._handler(job.kind, payload, job.owner)
                    await self.engine.complete_step(
                        step_id, worker_id=self._worker_id, claim_version=job.attempts
                    )
                    if not await self.engine.succeed(
                        job.id, worker_id=self._worker_id, claim_version=job.attempts
                    ):
                        await self.engine.confirm_cancelled(
                            job.id, self._worker_id, claim_version=job.attempts
                        )
                except MaintenanceSourceGone:
                    await self.engine.cancel(
                        job.id, worker_id=self._worker_id, claim_version=job.attempts
                    )
                    await self.engine.confirm_cancelled(
                        job.id, self._worker_id, claim_version=job.attempts
                    )
                except Exception as error:
                    logger.warning("maintenance failed job=%s kind=%s", job.id, job.kind)
                    if await self.engine.cancel_requested(job.id):
                        await self.engine.confirm_cancelled(
                            job.id, self._worker_id, claim_version=job.attempts
                        )
                    else:
                        await self.engine.fail_step(
                            step_id,
                            error_code=error.reason_code
                            if isinstance(error, BudgetDenied)
                            else self._error_code,
                            retryable=not isinstance(error, BudgetDenied)
                            or error.reason_code == "user_model_concurrency_exhausted",
                            worker_id=self._worker_id,
                            claim_version=job.attempts,
                        )
                finally:
                    _CURRENT_JOB.reset(context_token)
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat

    async def _renew(self, job_id: UUID, claim_version: int) -> None:
        while True:
            await asyncio.sleep(30)
            if not await self.engine.renew_lease(
                job_id, self._worker_id, lease_seconds=300, claim_version=claim_version
            ):
                return
