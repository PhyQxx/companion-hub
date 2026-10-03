"""Tool admission uses live time and holds its owner until commit."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select, update
from test_job_lifecycle_fence import during_commit
from test_run_budget import make_budget

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, Database, TaskRunEventRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, ToolPermit
from app.runs import resources
from app.runs.resources import RunToolBudget, resource_usage
from app.runs.store import append_run_event
from scripts.benchmark_storage import FixtureStorage, open_storage

BASE = datetime(2030, 1, 1, tzinfo=UTC)


async def prepared(
    backend: str, tmp_path: Path, phase: str = "interactive"
) -> tuple[FixtureStorage, RunToolBudget]:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "tools.db", url)
    try:
        config = RunBudgetConfig(max_tool_attempts=1)
        model = await make_budget(storage.database, config)
        deadline = BASE + timedelta(seconds=1)
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == model.run_id)
                .values(
                    deadline=deadline, status="succeeded" if phase == "maintenance" else "running"
                )
            )
        return storage, RunToolBudget(
            storage.database,
            run_id=model.run_id,
            user_id=model.owner_id,
            config=config,
            maintenance=phase == "maintenance",
            deadline=deadline,
        )
    except BaseException:
        await storage.close()
        raise


def clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    now = [BASE]

    class Clock:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return now[0]

    monkeypatch.setattr(resources, "datetime", Clock)
    return now


async def empty(database: Database, budget: RunToolBudget) -> None:
    async with database.sessions() as session:
        root = await session.get_one(TaskRunRecord, budget._run_id)
        assert resource_usage(root) == {}
        assert root.state_version == 1 and root.event_seq == 0
        assert list(await session.scalars(select(TaskRunEventRecord))) == []


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("phase", ["interactive", "maintenance"])
async def test_tool_expiration_during_run_lock_wait_does_not_consume_attempt(
    backend: str, phase: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, budget = await prepared(backend, tmp_path, phase)
    now = clock(monkeypatch)
    attempted = asyncio.Event()
    task: asyncio.Task[ToolPermit] | None = None
    installed = False

    def before_execute(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("UPDATE ") and any(
            budget._run_id in values.values() or budget._user_id in values.values()
            for values in context.compiled_parameters
        ):
            attempted.set()

    try:
        event.listen(storage.database.engine.sync_engine, "before_cursor_execute", before_execute)
        installed = True
        async with storage.database.sessions.begin() as writer:
            await writer.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == budget._run_id)
                .values(id=TaskRunRecord.id)
            )
            attempted.clear()
            task = asyncio.create_task(
                budget.reserve_tool(tool_name="fixture", user_id=budget._user_id)
            )
            await asyncio.wait_for(attempted.wait(), 3)
            assert not task.done()
            now[0] += timedelta(seconds=2)
        with pytest.raises(BudgetDenied, match="run_deadline_exceeded"):
            await asyncio.wait_for(task, 3)
        await empty(storage.database, budget)
    finally:
        if installed:
            event.remove(
                storage.database.engine.sync_engine, "before_cursor_execute", before_execute
            )
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("phase", ["interactive", "maintenance"])
async def test_tool_expiration_during_event_write_rolls_back(
    backend: str, phase: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, budget = await prepared(backend, tmp_path, phase)
    now = clock(monkeypatch)
    append = append_run_event

    async def slow_event(*args: Any, **kwargs: Any) -> None:
        await append(*args, **kwargs)
        now[0] += timedelta(seconds=2)

    monkeypatch.setattr(resources, "append_run_event", slow_event)
    try:
        with pytest.raises(BudgetDenied, match="run_deadline_exceeded"):
            await budget.reserve_tool(tool_name="fixture", user_id=budget._user_id)
        await empty(storage.database, budget)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_tool_waiting_owner_deactivation_cannot_admit(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, budget = await prepared(backend, tmp_path)
    clock(monkeypatch)

    async def rejected() -> None:
        with pytest.raises(BudgetDenied, match="budget_owner_invalid"):
            await budget.reserve_tool(tool_name="fixture", user_id=budget._user_id)

    try:
        await during_commit(
            storage.database,
            lambda session: session.execute(
                update(AppUserRecord)
                .where(AppUserRecord.id == budget._user_id)
                .values(status="inactive")
            ),
            rejected,
        )
        await empty(storage.database, budget)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_tool_owner_write_fence_is_held_until_acceptance_commit(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, budget = await prepared(backend, tmp_path)
    clock(monkeypatch)
    append = append_run_event
    attempted = asyncio.Event()
    task: asyncio.Task[None] | None = None
    installed = False

    def before_execute(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("UPDATE ") and any(
            values.get("status") == "inactive" for values in context.compiled_parameters
        ):
            attempted.set()

    async def deactivate() -> None:
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(AppUserRecord)
                .where(AppUserRecord.id == budget._user_id)
                .values(status="inactive")
            )

    async def before_commit(*args: Any, **kwargs: Any) -> None:
        nonlocal task
        await append(*args, **kwargs)
        task = asyncio.create_task(deactivate())
        await asyncio.wait_for(attempted.wait(), 3)
        await asyncio.sleep(0.05)
        assert not task.done(), "Owner deactivation committed inside the tool acceptance window"

    monkeypatch.setattr(resources, "append_run_event", before_commit)
    try:
        event.listen(storage.database.engine.sync_engine, "before_cursor_execute", before_execute)
        installed = True
        await budget.reserve_tool(tool_name="fixture", user_id=budget._user_id)
        assert task is not None
        await asyncio.wait_for(task, 3)
        async with storage.database.sessions() as session:
            owner = await session.get_one(AppUserRecord, budget._user_id)
            root = await session.get_one(TaskRunRecord, budget._run_id)
            assert owner.status == "inactive" and resource_usage(root)["tool_attempts"] == 1
    finally:
        if installed:
            event.remove(
                storage.database.engine.sync_engine, "before_cursor_execute", before_execute
            )
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await storage.close()
