"""The Run's committed budget controls admission after a row-lock wait."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, func, select, update
from test_job_lifecycle_fence import during_commit
from test_run_budget import make_budget

from app.config.models import RunBudgetConfig
from app.db import ModelReservationRecord, TaskRunRecord
from app.harness.budget import BudgetDenied
from app.llm.contracts import ModelPricing
from scripts.benchmark_storage import open_storage


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("changed", ["tokens", "attempts", "concurrency", "money", "enabled"])
async def test_waiting_admission_respects_committed_budget_snapshot(
    backend: str, changed: str, tmp_path: Path
) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "snapshot.db", url)
    database = storage.database
    pricing = ModelPricing(input_rate=1, output_rate=2, currency="CNY")
    try:
        config = RunBudgetConfig()
        budget = await make_budget(database, config)
        baseline_attempts, baseline_tokens, baseline_calls = 0, 0, 0
        if changed == "attempts":
            async with database.sessions.begin() as session:
                await session.execute(
                    update(TaskRunRecord)
                    .where(TaskRunRecord.id == budget.run_id)
                    .values(llm_attempts=2)
                )
            baseline_attempts = 2
        elif changed == "concurrency":
            await budget.reserve(endpoint="fixture", tokens=10, final=True, pricing=pricing)
            baseline_attempts, baseline_tokens, baseline_calls = 1, 10, 1
        variants: dict[str, dict[str, object]] = {
            "tokens": {"max_tokens": 1024},
            "attempts": {"max_llm_attempts": 2},
            "concurrency": {"max_concurrent_llm_calls": 1},
            "money": {"cost_currency": "CNY", "max_daily_cost": 0},
            "enabled": {"enabled": False},
        }
        updates = variants[changed]
        committed = RunBudgetConfig.model_validate({**config.model_dump(), **updates})
        reason = {
            "tokens": "run_budget_exhausted",
            "attempts": "run_budget_exhausted",
            "concurrency": "user_model_concurrency_exhausted",
            "money": "daily_cost_budget_exhausted",
            "enabled": "budget_snapshot_missing",
        }[changed]

        async def rejected() -> None:
            with pytest.raises(BudgetDenied, match=reason):
                await budget.reserve(
                    endpoint="fixture",
                    tokens=2000 if changed == "tokens" else 40,
                    final=True,
                    pricing=pricing,
                )

        await during_commit(
            database,
            lambda session: session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == budget.run_id)
                .values(budget=committed.model_dump())
            ),
            rejected,
        )
        async with database.sessions() as session:
            root = await session.get_one(TaskRunRecord, budget.run_id)
            assert root.budget == committed.model_dump()
            assert root.llm_attempts == baseline_attempts and root.budget_tokens == baseline_tokens
            assert (
                await session.scalar(select(func.count(ModelReservationRecord.call_id)))
                == baseline_calls
            )
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_successful_model_admission_does_not_preread_snapshot_or_concurrency(
    backend: str, tmp_path: Path
) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "statements.db", url)
    database = storage.database
    statements: list[str] = []
    installed = False

    def before_execute(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    try:
        budget = await make_budget(database, RunBudgetConfig())
        event.listen(database.engine.sync_engine, "before_cursor_execute", before_execute)
        installed = True
        permit = await budget.reserve(endpoint="fixture", tokens=40, final=True)
        event.remove(database.engine.sync_engine, "before_cursor_execute", before_execute)
        installed = False
        # The direct fixture has no parent claim or money cap. Its entire
        # acceptance is owner UPDATE, conditional Run UPDATE and two inserts.
        assert len(statements) == 4
        assert all(
            statement.lstrip().upper().startswith(("UPDATE ", "INSERT "))
            for statement in statements
        )
        async with database.sessions() as session:
            root = await session.get_one(TaskRunRecord, budget.run_id)
            reservation = await session.get_one(ModelReservationRecord, permit.call_id)
            assert root.llm_attempts == 1 and root.budget_tokens == 40
            assert reservation.state == "reserved" and reservation.reserved_tokens == 40
    finally:
        if installed:
            event.remove(database.engine.sync_engine, "before_cursor_execute", before_execute)
        await storage.close()
