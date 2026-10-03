"""Durable single-attempt admission without storing semantic event bodies."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from uuid import UUID

from sqlalchemy import or_, select, update

from app.cognition import SemanticEvent
from app.db import AppUserRecord, Database, JobRecord, SemanticEventAuditRecord
from app.db.claims import assert_current_claim
from app.harness.budget import BudgetDenied
from app.harness.claim import ClaimInvalidated, ExecutionClaim, claim_scope, current_claims
from app.harness.guarded_call import guarded_call
from app.harness.time import utc
from app.ids import uuid7
from app.jobs.engine import JobEngine, JobView

T = TypeVar("T")
_RESOURCE = "perception-inline"


def _key(event: SemanticEvent) -> str:
    return f"perception:{event.event_id}"


def _payload(event: SemanticEvent) -> dict[str, str]:
    canonical = event.model_copy(
        update={
            "occurred_at": utc(event.occurred_at),
            "expires_at": utc(event.expires_at) if event.expires_at is not None else None,
        }
    )
    body = json.dumps(canonical.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return {
        "event_id": str(event.event_id),
        "dedupe_hash": hashlib.sha256(
            (event.dedupe_key or f"kind:{event.kind}").encode()
        ).hexdigest(),
        "fingerprint": hashlib.sha256(body.encode()).hexdigest(),
    }


class EventAdmission:
    def __init__(self, database: Database, *, window_seconds: int) -> None:
        self._database = database
        self._engine = JobEngine(database)
        self._window_seconds = window_seconds

    async def verify(self, event: SemanticEvent) -> None:
        async with self._database.sessions() as session:
            job = await session.scalar(
                select(JobRecord).where(JobRecord.idempotency_key == _key(event))
            )
            if job is not None:
                if job.owner != str(event.user_id):
                    raise BudgetDenied("event_source_owner_mismatch")
                if job.kind != "perception.event" or job.input != _payload(event):
                    raise BudgetDenied("event_source_changed")

    async def _admit(self, event: SemanticEvent) -> JobView:
        payload = _payload(event)
        async with self._database.sessions.begin() as session:
            await assert_current_claim(session)
            # A short owner-row write serializes reservations on both PostgreSQL
            # and SQLite. Never hold this transaction during model/provider calls.
            owner = await session.execute(
                update(AppUserRecord)
                .where(AppUserRecord.id == event.user_id, AppUserRecord.status == "active")
                .values(status=AppUserRecord.status)
                .returning(AppUserRecord.id)
            )
            if owner.scalar_one_or_none() is None:
                raise BudgetDenied("event_owner_inactive")
            existing = await session.scalar(
                select(JobRecord).where(JobRecord.idempotency_key == _key(event))
            )
            if existing is not None:
                if existing.owner != str(event.user_id):
                    raise BudgetDenied("event_source_owner_mismatch")
                if existing.kind != "perception.event" or existing.input != payload:
                    raise BudgetDenied("event_source_changed")
            else:
                now = datetime.now(UTC)
                previous = await session.scalar(
                    select(JobRecord)
                    .where(
                        JobRecord.owner == str(event.user_id),
                        JobRecord.kind == "perception.event",
                        JobRecord.input["dedupe_hash"].as_string() == payload["dedupe_hash"],
                        or_(
                            JobRecord.created_at >= now - timedelta(seconds=self._window_seconds),
                            JobRecord.status.in_({"admitted", "running", "cancelling"})
                            & (JobRecord.lease_expires_at > now),
                        ),
                    )
                    .order_by(JobRecord.created_at.desc(), JobRecord.id.desc())
                    .limit(1)
                )
                if previous is not None:
                    audit = await session.get(
                        SemanticEventAuditRecord, UUID(str(previous.input["event_id"]))
                    )
                    if audit is None:
                        raise BudgetDenied("event_dedupe_in_flight_or_unknown")
            return await self._engine.submit_in_session(
                session,
                "perception.event",
                payload,
                owner=str(event.user_id),
                idempotency_key=_key(event),
                resource_class=_RESOURCE,
                max_attempts=1,
            )

    async def execute(self, event: SemanticEvent, invoke: Callable[[], Awaitable[T]]) -> T:
        job = await self._admit(event)
        worker = f"perception-{uuid7()}"
        claimed = await self._engine.claim(worker, resource_class=_RESOURCE, job_id=job.id)
        if claimed is None:
            # A crash, timeout or cancellation must never cause this source to run again.
            raise BudgetDenied("event_source_already_admitted")

        async def check() -> None:
            for claim in current_claims():
                if not await self._engine.claim_active(
                    claim.job_id, claim.worker_id, claim.version
                ):
                    raise ClaimInvalidated()

        async def renew() -> None:
            while True:
                await asyncio.sleep(30)
                if not await self._engine.renew_lease(
                    job.id, worker, claim_version=claimed.attempts
                ):
                    return

        heartbeat: asyncio.Task[None] | None = None
        try:
            with claim_scope(ExecutionClaim(job.id, worker, claimed.attempts)):
                heartbeat = asyncio.create_task(renew())
                result = await guarded_call(invoke, check)
                if not await self._engine.succeed(
                    job.id, worker_id=worker, claim_version=claimed.attempts
                ):
                    raise ClaimInvalidated()
                return result
        except BaseException:
            # This terminal status describes the worker lifecycle, not proof that
            # every side effect was rolled back. max_attempts=1 also fences crashes.
            await self._engine.cancel(job.id, worker_id=worker, claim_version=claimed.attempts)
            await self._engine.confirm_cancelled(job.id, worker, claim_version=claimed.attempts)
            raise
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
