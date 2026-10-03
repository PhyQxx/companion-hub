"""Lock admission time determines spending periods and deadline rejection."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, func, select, update
from test_run_budget import make_budget

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, Database, ModelCostRecord, ModelReservationRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, CallPermit
from app.llm.contracts import ModelPricing
from app.runs import budget as budget_storage
from app.runs.budget import RunModelBudget
from scripts.benchmark_storage import FixtureStorage, open_storage

BASE = datetime(2030, 1, 31, 23, 59, 59, tzinfo=UTC)
PRICING = ModelPricing(input_rate=1, output_rate=2, currency="CNY")


async def prepared(
    backend: str, tmp_path: Path, config: RunBudgetConfig, *, deadline: datetime
) -> tuple[FixtureStorage, RunModelBudget]:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "flush.db", url)
    try:
        budget = await make_budget(storage.database, config)
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == budget.run_id)
                .values(deadline=deadline)
            )
        return storage, budget
    except BaseException:
        await storage.close()
        raise


def clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    now = [BASE]

    class Clock:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return now[0]

    monkeypatch.setattr(budget_storage, "datetime", Clock)
    return now


async def assert_no_reservation(database: Database, budget: RunModelBudget) -> None:
    async with database.sessions() as session:
        root = await session.get_one(TaskRunRecord, budget.run_id)
        assert root.llm_attempts == 0 and root.budget_tokens == 0
        assert await session.scalar(select(func.count(ModelCostRecord.call_id))) == 0
        assert await session.scalar(select(func.count(ModelReservationRecord.call_id))) == 0


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("locked", ["owner", "run"])
@pytest.mark.parametrize("boundary", ["day", "month", "deadline"])
async def test_waiting_model_admission_uses_fresh_time(
    backend: str,
    locked: str,
    boundary: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "admission.db", url)
    database = storage.database
    now = [BASE]

    class Clock:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return now[0]

    attempted = asyncio.Event()
    task: asyncio.Task[CallPermit] | None = None
    installed = False
    try:
        config = RunBudgetConfig(
            cost_currency="CNY",
            max_daily_cost=0.0008 if boundary == "day" else None,
            max_monthly_cost=0.0008 if boundary == "month" else None,
        )
        budget = await make_budget(database, config)
        async with database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == budget.run_id)
                .values(
                    deadline=BASE
                    + (timedelta(seconds=1) if boundary == "deadline" else timedelta(hours=1))
                )
            )
        table = AppUserRecord if locked == "owner" else TaskRunRecord
        identifier = budget.owner_id if locked == "owner" else budget.run_id
        # SQLite's single writer blocks the first owner UPDATE even when the
        # held row is the Run. PostgreSQL waits on the particular held row.
        observed = budget.owner_id if backend == "sqlite" else identifier

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

        monkeypatch.setattr(budget_storage, "datetime", Clock)
        event.listen(database.engine.sync_engine, "before_cursor_execute", before_execute)
        installed = True
        async with database.sessions.begin() as writer:
            await writer.execute(update(table).where(table.id == identifier).values(id=table.id))
            attempted.clear()
            task = asyncio.create_task(
                budget.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)
            )
            await asyncio.wait_for(attempted.wait(), 3)
            assert not task.done()
            now[0] += timedelta(seconds=2)
        if boundary == "deadline":
            with pytest.raises(BudgetDenied, match="run_deadline_exceeded"):
                await asyncio.wait_for(task, 3)
            async with database.sessions() as session:
                root = await session.get_one(TaskRunRecord, budget.run_id)
                assert root.llm_attempts == root.budget_tokens == 0
                assert await session.scalar(select(func.count(ModelCostRecord.call_id))) == 0
                assert await session.scalar(select(func.count(ModelReservationRecord.call_id))) == 0
        else:
            first = await asyncio.wait_for(task, 3)
            reason = (
                "daily_cost_budget_exhausted"
                if boundary == "day"
                else "monthly_cost_budget_exhausted"
            )
            with pytest.raises(BudgetDenied, match=reason):
                await budget.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)
            async with database.sessions() as session:
                cost = await session.get_one(ModelCostRecord, first.call_id)
                reservation = await session.get_one(ModelReservationRecord, first.call_id)
                assert cost.created_at.replace(tzinfo=UTC) == now[0]
                assert reservation.created_at.replace(tzinfo=UTC) == now[0]
                root = await session.get_one(TaskRunRecord, budget.run_id)
                assert root.llm_attempts == 1 and root.budget_tokens == 400
    finally:
        if installed:
            event.remove(database.engine.sync_engine, "before_cursor_execute", before_execute)
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("boundary", ["day", "month", "deadline"])
async def test_flushing_across_budget_boundary_rolls_back_before_permit(
    backend: str, boundary: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RunBudgetConfig(
        cost_currency="CNY",
        max_daily_cost=0.0008 if boundary == "day" else None,
        max_monthly_cost=0.0008 if boundary == "month" else None,
    )
    storage, budget = await prepared(
        backend,
        tmp_path,
        config,
        deadline=BASE + (timedelta(seconds=1) if boundary == "deadline" else timedelta(hours=1)),
    )
    now = clock(monkeypatch)

    def after_execute(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("INSERT ") and "model_reservation" in statement:
            now[0] += timedelta(seconds=2)

    event.listen(storage.database.engine.sync_engine, "after_cursor_execute", after_execute)
    try:
        reason = "run_deadline_exceeded" if boundary == "deadline" else "cost_window_changed"
        with pytest.raises(BudgetDenied, match=reason):
            await budget.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)
        await assert_no_reservation(storage.database, budget)
    finally:
        event.remove(storage.database.engine.sync_engine, "after_cursor_execute", after_execute)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("priced", [False, True])
async def test_unissued_postcommit_expiry_records_confirmed_zero_usage(
    backend: str, priced: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, budget = await prepared(
        backend, tmp_path, RunBudgetConfig(), deadline=BASE + timedelta(seconds=1)
    )
    now = clock(monkeypatch)
    armed = [True]

    def committing(connection: Any) -> None:
        if armed[0]:
            armed[0] = False
            now[0] += timedelta(seconds=2)

    event.listen(storage.database.engine.sync_engine, "commit", committing)
    try:
        with pytest.raises(BudgetDenied, match="run_deadline_exceeded"):
            await budget.reserve(
                endpoint="fixture", tokens=400, final=True, pricing=PRICING if priced else None
            )
        async with storage.database.sessions() as session:
            cost = (await session.scalars(select(ModelCostRecord))).one()
            reservation = (await session.scalars(select(ModelReservationRecord))).one()
            assert cost.state == "estimated" and cost.charged_micros == 0
            assert cost.provider_request_id is None
            assert reservation.state == "settled" and reservation.actual_tokens == 0
            assert reservation.charged_tokens == 0
            root = await session.get_one(TaskRunRecord, budget.run_id)
            # An admission occurred; an expired permit never reached a provider.
            assert root.llm_attempts == 1 and root.budget_tokens == 0
    finally:
        event.remove(storage.database.engine.sync_engine, "commit", committing)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_waiting_admission_rechecks_the_committed_run_deadline(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, budget = await prepared(
        backend, tmp_path, RunBudgetConfig(), deadline=BASE + timedelta(hours=1)
    )
    now = clock(monkeypatch)
    attempted = asyncio.Event()
    observed = budget.owner_id if backend == "sqlite" else budget.run_id
    task: asyncio.Task[CallPermit] | None = None

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

    event.listen(storage.database.engine.sync_engine, "before_cursor_execute", before_execute)
    try:
        async with storage.database.sessions.begin() as writer:
            await writer.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == budget.run_id)
                .values(deadline=BASE + timedelta(seconds=1))
            )
            attempted.clear()
            task = asyncio.create_task(
                budget.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)
            )
            await asyncio.wait_for(attempted.wait(), 3)
            now[0] += timedelta(seconds=2)
        with pytest.raises(BudgetDenied, match="run_deadline_exceeded"):
            await asyncio.wait_for(task, 3)
        await assert_no_reservation(storage.database, budget)
    finally:
        event.remove(storage.database.engine.sync_engine, "before_cursor_execute", before_execute)
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await storage.close()
