"""Scheduled job enrollment starts with a write under SQLite transactions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_delivery_sqlite_transactions import explicit_transactions
from test_jobs import database as job_database
from test_jobs import tmp_db as job_temporary
from test_self_check import (
    RecordingDeliverer,
    _scheduler,
)
from test_self_check import (
    test_self_check_slot_survives_restart_and_competing_schedulers as check_schedulers,
)

from app.db import Database
from app.jobs import JobEngine

database = job_database
tmp_db = job_temporary


async def test_competing_scheduled_enrollment_does_not_upgrade_shared_reads(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers = 0
    both_read = asyncio.Event()
    scalar = AsyncSession.scalar

    async def intercepted(self: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal readers
        result = await scalar(self, statement, *args, **kwargs)
        if statement.is_select and "job.idempotency_key" in str(statement) and result is None:
            readers += 1
            if readers == 2:
                both_read.set()
            await asyncio.wait_for(both_read.wait(), 2)
        return result

    monkeypatch.setattr(AsyncSession, "scalar", intercepted)
    engine = JobEngine(database)
    async with explicit_transactions(database):
        jobs = await asyncio.wait_for(
            asyncio.gather(
                *(
                    engine.submit_scheduled(
                        "fixture.schedule",
                        {},
                        schedule_id="fixture",
                        scheduled_slot=datetime(2026, 10, 3, tzinfo=UTC),
                        owner="fixture",
                        resource_class="fixture",
                    )
                    for _ in range(2)
                ),
                return_exceptions=True,
            ),
            8,
        )
    accepted = [job for job in jobs if not isinstance(job, BaseException)]
    assert len(accepted) == 2, jobs
    assert accepted[0].id == accepted[1].id
    assert await engine.count_jobs(kind="fixture.schedule") == 1


async def test_competing_self_checks_use_explicit_sqlite_transactions(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The original check needs no provider or device: it verifies that the
    # unique daily slot survives competing schedulers and a restart.
    async with explicit_transactions(database):
        await check_schedulers(database, monkeypatch)
    assert await _scheduler(database, RecordingDeliverer()).run_once() == []
