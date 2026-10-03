"""Time spent waiting for row locks must not keep an expired ancestor claim alive."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select, update
from test_claim_transaction import claim
from test_observation_owner_binding import accounts

from app.db import AppUserRecord, JobRecord
from app.db import claims as claim_storage
from app.harness.claim import ClaimInvalidated, claim_scope
from scripts.benchmark_storage import open_storage


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("scope", ["root", "parent"])
async def test_claim_expiry_is_rechecked_after_lock_wait(
    backend: str, scope: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "wait.db", url)
    database = storage.database
    task: asyncio.Task[None] | None = None
    installed = False
    now = [datetime.now(UTC)]

    class Clock:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return now[0]

    attempted = asyncio.Event()

    try:
        owner, _ = await accounts(database)
        _, root = await claim(database, owner)
        _, leaf = await claim(database, owner)
        held = leaf.job_id if scope == "parent" else root.job_id
        # SQLite has one database writer: the root is already blocked by a
        # writer on the leaf. PostgreSQL gets as far as locking the leaf.
        observed = held if backend == "postgresql" else root.job_id
        async with database.sessions.begin() as session:
            await session.execute(
                update(JobRecord)
                .where(JobRecord.id == root.job_id)
                .values(lease_expires_at=now[0] + timedelta(seconds=1))
            )
            await session.execute(
                update(JobRecord)
                .where(JobRecord.id == leaf.job_id)
                .values(lease_expires_at=now[0] + timedelta(seconds=10))
            )
        monkeypatch.setattr(claim_storage, "datetime", Clock)

        def before_execute(
            connection: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            if statement.lstrip().upper().startswith("UPDATE ") and any(
                observed in values.values() for values in context.compiled_parameters
            ):
                attempted.set()

        event.listen(database.engine.sync_engine, "before_cursor_execute", before_execute)
        installed = True

        async def derived() -> None:
            async with database.sessions.begin() as session:
                await claim_storage.assert_current_claim(session)
                await session.execute(
                    update(AppUserRecord)
                    .where(AppUserRecord.id == owner)
                    .values(display_name="Derived after expiry")
                )

        async with database.sessions.begin() as writer:
            await writer.execute(update(JobRecord).where(JobRecord.id == held).values(progress=0.1))
            attempted.clear()
            with claim_scope(root):
                if scope == "parent":
                    with claim_scope(leaf):
                        task = asyncio.create_task(derived())
                else:
                    task = asyncio.create_task(derived())
            await asyncio.wait_for(attempted.wait(), 2)
            assert not task.done()
            now[0] += timedelta(seconds=2)
        with pytest.raises(ClaimInvalidated):
            await asyncio.wait_for(task, 2)
        async with database.sessions.begin() as session:
            assert (
                await session.scalar(
                    select(AppUserRecord.display_name).where(AppUserRecord.id == owner)
                )
                == "First"
            )
    finally:
        if installed:
            event.remove(database.engine.sync_engine, "before_cursor_execute", before_execute)
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await storage.close()
