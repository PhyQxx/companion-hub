"""Duplicate claims reuse the accepted Run without colliding inserts."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from test_delivery_sqlite_transactions import explicit_transactions
from test_goal_delivery_runs import seed

from app.tasks.goal_scheduler import GoalReminderScheduler
from scripts.benchmark_storage import open_storage


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
async def test_goal_replay_checks_existing_run_under_source_lock(
    backend: str, tmp_path: Path
) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "replay.db", url)
    database = storage.database
    started, release = asyncio.Event(), asyncio.Event()
    inserts: list[str] = []
    installed = False
    first: asyncio.Task[None] | None = None
    calls = 0

    def before_execute(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO ") and "task_run (" in statement:
            inserts.append(statement)

    @asynccontextmanager
    async def transactions() -> AsyncIterator[None]:
        if backend == "sqlite_explicit":
            async with explicit_transactions(database):
                yield
        else:
            yield

    try:
        async with transactions():
            _, store, _ = await seed(database)
            item = (await store.claim_due_goal_reminders())[0]

            async def dispatch(text: str, **kwargs: object) -> list[str]:
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()
                return ["web_chat"]

            first = asyncio.create_task(
                GoalReminderScheduler(store, deliverer=dispatch)._remind(item)
            )
            await asyncio.wait_for(started.wait(), 3)
            event.listen(database.engine.sync_engine, "before_cursor_execute", before_execute)
            installed = True
            schedulers = [GoalReminderScheduler(store, deliverer=dispatch) for _ in range(7)]
            results = await asyncio.gather(
                *(scheduler._remind(item) for scheduler in schedulers), return_exceptions=True
            )
            assert results == [None] * 7
            assert calls == 1
            assert inserts == [], "Replays must not insert a conflicting Run and roll it back"
    finally:
        if installed:
            event.remove(database.engine.sync_engine, "before_cursor_execute", before_execute)
        release.set()
        if first is not None:
            await asyncio.gather(first, return_exceptions=True)
        await storage.close()
