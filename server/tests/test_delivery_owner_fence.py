"""Delivery acceptance keeps owner validation valid through both commits."""

import asyncio
import os
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_delivery_runs import seed
from test_job_lifecycle_fence import during_commit

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, DailyBriefRecord, TaskRunEventRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, ToolPermit
from app.runs import delivery
from app.runs.delivery_contracts import DeliverySourceSnapshot
from app.runs.delivery_sources import SqlDeliverySourceRepository
from app.runs.resources import RunToolBudget
from scripts.benchmark_storage import FixtureStorage, open_storage


async def prepared(backend: str, tmp_path: Path) -> FixtureStorage:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    return await open_storage(tmp_path / "delivery.db", url)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_delivery_waiting_owner_deactivation_rejects_before_acceptance(
    backend: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    calls = 0

    async def dispatch() -> list[str]:
        nonlocal calls
        calls += 1
        return ["web"]

    try:
        owner, source = await seed(storage.database, DailyBriefRecord)

        async def attempt() -> None:
            with pytest.raises(BudgetDenied, match="budget_owner_invalid"):
                await delivery.deliver_once(
                    storage.database,
                    table=DailyBriefRecord,
                    source_id=source,
                    user_id=owner,
                    text="private synthetic body",
                    entry="fixture.delivery",
                    config=RunBudgetConfig(enabled=False),
                    dispatch=dispatch,
                )

        await during_commit(
            storage.database,
            lambda session: session.execute(
                update(AppUserRecord).where(AppUserRecord.id == owner).values(status="inactive")
            ),
            attempt,
        )
        async with storage.database.sessions() as session:
            assert await session.get(TaskRunRecord, source) is None
            assert list(await session.scalars(select(TaskRunEventRecord))) == []
        assert calls == 0
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("stage", [1, 2])
async def test_delivery_owner_fence_is_held_until_stage_commit(
    backend: str, stage: int, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    attempted = asyncio.Event()
    task: asyncio.Task[None] | None = None
    inspections = 0
    committed_inside: list[bool] = []

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

    async def dispatch() -> list[str]:
        return ["web"]

    try:
        owner, source = await seed(storage.database, DailyBriefRecord)

        async def deactivate() -> None:
            async with storage.database.sessions.begin() as session:
                await session.execute(
                    update(AppUserRecord).where(AppUserRecord.id == owner).values(status="inactive")
                )

        class Repository(SqlDeliverySourceRepository):
            async def inspect(
                self, session: AsyncSession, fingerprint: str, run_id: UUID
            ) -> DeliverySourceSnapshot:
                nonlocal task, inspections
                snapshot = await super().inspect(session, fingerprint, run_id)
                inspections += 1
                if inspections == stage:
                    task = asyncio.create_task(deactivate())
                    await asyncio.wait_for(attempted.wait(), 3)
                    await asyncio.sleep(0.05)
                    committed_inside.append(task.done())
                return snapshot

        event.listen(storage.database.engine.sync_engine, "before_cursor_execute", before_execute)
        try:
            # Deactivation after acceptance may be observed by the live watch.
            with suppress(BudgetDenied):
                await delivery.deliver_once(
                    storage.database,
                    source_repository=Repository(DailyBriefRecord, source, owner),
                    source_id=source,
                    user_id=owner,
                    text="private synthetic body",
                    entry="fixture.delivery",
                    config=RunBudgetConfig(enabled=False),
                    dispatch=dispatch,
                )
            assert inspections >= stage and task is not None
            await asyncio.wait_for(task, 3)
            assert committed_inside == [False], "Owner deactivated inside delivery acceptance"
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
async def test_owner_deactivated_after_tool_admission_does_not_mark_dispatch_started(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await prepared(backend, tmp_path)
    reserve = RunToolBudget.reserve_tool
    calls = 0

    async def dispatch() -> list[str]:
        nonlocal calls
        calls += 1
        return ["web"]

    async def reserve_then_deactivate(
        self: RunToolBudget, *, tool_name: str, user_id: UUID | None
    ) -> ToolPermit:
        permit = await reserve(self, tool_name=tool_name, user_id=user_id)
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(AppUserRecord).where(AppUserRecord.id == user_id).values(status="inactive")
            )
        return permit

    monkeypatch.setattr(RunToolBudget, "reserve_tool", reserve_then_deactivate)
    try:
        owner, source = await seed(storage.database, DailyBriefRecord)
        with pytest.raises(BudgetDenied, match="budget_owner_invalid"):
            await delivery.deliver_once(
                storage.database,
                table=DailyBriefRecord,
                source_id=source,
                user_id=owner,
                text="private synthetic body",
                entry="fixture.delivery",
                config=RunBudgetConfig(),
                dispatch=dispatch,
            )
        assert calls == 0
        async with storage.database.sessions() as session:
            row = await session.get_one(TaskRunRecord, source)
            assert row.status == "failed" and row.contract["dispatch_state"] == "not_started"
            kinds = list(await session.scalars(select(TaskRunEventRecord.kind)))
            assert "run.delivery.started" not in kinds
    finally:
        await storage.close()
