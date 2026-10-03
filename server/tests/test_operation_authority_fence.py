"""Provider operations validate live authority inside their write transactions."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, select, update
from test_resource_budget import seed

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, ModelCostRecord, TaskRunEventRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, budget_scope
from app.harness.operations import OperationPolicy
from app.harness.time import utc
from app.ids import uuid7
from app.runs import operation
from app.runs.budget import RunModelBudget
from app.runs.store import append_run_event, transition_run
from app.schemas import PrivacyLevel
from scripts.benchmark_storage import FixtureStorage, open_storage


async def prepared(backend: str, tmp_path: Path) -> FixtureStorage:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    return await open_storage(tmp_path / "operation.db", url)


def policy() -> OperationPolicy:
    return OperationPolicy(1, tuple(RunBudgetConfig(enabled=False).model_dump(mode="json").items()))


async def execute(
    storage: FixtureStorage,
    owner: UUID,
    invoke: Callable[[Callable[[], Awaitable[None]]], Awaitable[str]],
    guard: Callable[[], Awaitable[None]],
) -> str:
    return await operation.operate_with_run(
        storage.database,
        policy(),
        user_id=owner,
        privacy_level=PrivacyLevel.L1,
        entry="fixture.operation",
        invoke=invoke,
        evidence=lambda result: {"provider_request_id": result},
        source_guard=guard,
        cost_endpoint="fixture",
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("stage", ["accept", "start"])
@pytest.mark.parametrize("change", ["owner", "cancel", "privacy", "cost", "deadline"])
async def test_operation_rechecks_authority_between_guard_and_write(
    backend: str, stage: str, change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await prepared(backend, tmp_path)
    inside_start = False
    revoked = False
    calls = 0
    try:
        owner, parent, _ = await seed(storage.database)
        parent_budget = RunModelBudget(
            storage.database, run_id=parent, user_id=owner, config=RunBudgetConfig(enabled=False)
        )

        async def revoke() -> None:
            nonlocal revoked
            revoked = True
            async with storage.database.sessions.begin() as session:
                if change == "owner":
                    await session.execute(
                        update(AppUserRecord)
                        .where(AppUserRecord.id == owner)
                        .values(status="inactive")
                    )
                else:
                    variants: dict[str, dict[str, Any]] = {
                        "cancel": {"status": "cancelled"},
                        "privacy": {"privacy_level": "L2"},
                        "cost": {"budget": {"cost_currency": "CNY", "max_daily_cost": 1}},
                        "deadline": {"deadline": datetime.now(UTC) - timedelta(seconds=1)},
                    }
                    await session.execute(
                        update(TaskRunRecord)
                        .where(TaskRunRecord.id == parent)
                        .values(**variants[change])
                    )

        async def recovery(database: Any) -> int:
            if stage == "accept":
                await revoke()
            return 0

        async def guard() -> None:
            if stage == "start" and inside_start and not revoked:
                await revoke()

        async def invoke(mark_started: Callable[[], Awaitable[None]]) -> str:
            nonlocal inside_start, calls
            inside_start = True
            await mark_started()
            calls += 1
            return "fixture-receipt"

        monkeypatch.setattr(operation, "recover_expired_operations", recovery)
        expected = {
            "owner": "budget_owner_invalid",
            "cancel": "budget_run_inactive",
            "privacy": "operation_privacy_downgrade",
            "cost": "media_cost_estimate_unavailable",
            "deadline": "run_deadline_exceeded",
        }[change]
        with budget_scope(parent_budget), pytest.raises(BudgetDenied, match=expected):
            await execute(storage, owner, invoke, guard)
        assert revoked and calls == 0
        async with storage.database.sessions() as session:
            children = list(
                await session.scalars(
                    select(TaskRunRecord).where(TaskRunRecord.parent_run_id == parent)
                )
            )
            if stage == "accept":
                assert children == []
            else:
                assert len(children) == 1 and children[0].status == "failed"
                assert children[0].contract["operation_state"] == "not_started"
            assert list(await session.scalars(select(ModelCostRecord))) == []
            kinds = list(await session.scalars(select(TaskRunEventRecord.kind)))
            assert "run.provider.started" not in kinds
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("kind", ["run.accepted", "run.provider.started", "run.succeeded"])
@pytest.mark.parametrize("authority", ["owner", "parent"])
async def test_operation_owner_is_locked_through_each_commit(
    backend: str, kind: str, authority: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await prepared(backend, tmp_path)
    attempted = asyncio.Event()
    task: asyncio.Task[None] | None = None
    overlaps: list[bool] = []

    def before_execute(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("UPDATE ") and any(
            values.get("status") in {"inactive", "cancelled"}
            for values in context.compiled_parameters
        ):
            attempted.set()

    async def guard() -> None:
        pass

    async def invoke(mark_started: Callable[[], Awaitable[None]]) -> str:
        await mark_started()
        return "fixture-receipt"

    try:
        owner, parent, _ = await seed(storage.database)
        parent_budget = RunModelBudget(
            storage.database, run_id=parent, user_id=owner, config=RunBudgetConfig(enabled=False)
        )

        async def deactivate() -> None:
            async with storage.database.sessions.begin() as session:
                if authority == "owner":
                    await session.execute(
                        update(AppUserRecord)
                        .where(AppUserRecord.id == owner)
                        .values(status="inactive")
                    )
                else:
                    await session.execute(
                        update(TaskRunRecord)
                        .where(TaskRunRecord.id == parent)
                        .values(status="cancelled")
                    )

        async def before_commit(event_kind: str) -> None:
            nonlocal task
            if event_kind == kind:
                task = asyncio.create_task(deactivate())
                await asyncio.wait_for(attempted.wait(), 3)
                await asyncio.sleep(0.05)
                overlaps.append(task.done())

        async def append(*args: Any, **kwargs: Any) -> None:
            await append_run_event(*args, **kwargs)
            await before_commit(args[2])

        async def transition(*args: Any, **kwargs: Any) -> None:
            await transition_run(*args, **kwargs)
            await before_commit(f"run.{args[2]}")

        monkeypatch.setattr(operation, "append_run_event", append)
        monkeypatch.setattr(operation, "transition_run", transition)
        event.listen(storage.database.engine.sync_engine, "before_cursor_execute", before_execute)
        try:
            with budget_scope(parent_budget), suppress(BudgetDenied):
                await execute(storage, owner, invoke, guard)
            assert task is not None
            await asyncio.wait_for(task, 3)
            assert overlaps == [False], "Authority changed during operation acceptance"
        finally:
            event.remove(
                storage.database.engine.sync_engine, "before_cursor_execute", before_execute
            )
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("kind", ["run.accepted", "run.provider.started"])
async def test_operation_deadline_expiring_during_event_rolls_back_unissued_state(
    backend: str, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await prepared(backend, tmp_path)
    now = [datetime(2030, 1, 1, tzinfo=UTC)]
    calls = 0

    class Clock:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return now[0]

    async def guard() -> None:
        pass

    async def invoke(mark_started: Callable[[], Awaitable[None]]) -> str:
        nonlocal calls
        await mark_started()
        calls += 1
        return "fixture-receipt"

    async def append(*args: Any, **kwargs: Any) -> None:
        await append_run_event(*args, **kwargs)
        if args[2] == kind:
            now[0] = utc(args[1].deadline) + timedelta(seconds=1)

    monkeypatch.setattr(operation, "datetime", Clock)
    monkeypatch.setattr(operation, "append_run_event", append)
    try:
        owner = uuid7()
        async with storage.database.sessions.begin() as session:
            session.add(AppUserRecord(id=owner, display_name="Fixture", status="active"))
        with pytest.raises(BudgetDenied, match="run_deadline_exceeded"):
            await execute(storage, owner, invoke, guard)
        assert calls == 0
        async with storage.database.sessions() as session:
            rows = list(await session.scalars(select(TaskRunRecord)))
            if kind == "run.accepted":
                assert rows == []
            else:
                assert len(rows) == 1 and rows[0].contract["operation_state"] == "not_started"
            assert list(await session.scalars(select(ModelCostRecord))) == []
    finally:
        await storage.close()
