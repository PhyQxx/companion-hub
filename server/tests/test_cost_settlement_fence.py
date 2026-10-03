"""Persistent fees serialize late settlement even after the source Run is deleted."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Select
from test_delivery_sqlite_transactions import explicit_transactions
from test_job_lifecycle_fence import during_commit
from test_run_budget import make_budget

from app.config.models import RunBudgetConfig
from app.db import ModelCostRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, CallPermit
from app.llm.contracts import ModelPricing, ModelUsage
from app.runs.budget import RunModelBudget
from scripts.benchmark_storage import FixtureStorage, open_storage


async def prepared(
    backend: str, tmp_path: Path
) -> tuple[FixtureStorage, RunModelBudget, CallPermit]:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "fees.db", url)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        permit = await budget.reserve(
            endpoint="fixture",
            tokens=100,
            final=True,
            pricing=ModelPricing(input_rate=1, output_rate=2, currency="CNY"),
        )
        async with storage.database.sessions.begin() as session:
            await session.execute(delete(TaskRunRecord).where(TaskRunRecord.id == budget.run_id))
        return storage, budget, permit
    except BaseException:
        await storage.close()
        raise


@asynccontextmanager
async def transaction_mode(storage: FixtureStorage, backend: str) -> AsyncIterator[None]:
    if backend == "sqlite_explicit":
        async with explicit_transactions(storage.database):
            yield
    else:
        yield


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
@pytest.mark.parametrize("conflict", ["amount", "receipt"])
async def test_late_fee_settlement_checks_committed_receipt_after_lock_wait(
    backend: str, conflict: str, tmp_path: Path
) -> None:
    storage, budget, permit = await prepared(backend, tmp_path)
    usage = ModelUsage(
        input_tokens=30 if conflict == "amount" else 20,
        total_tokens=30 if conflict == "amount" else 20,
        usage_known=True,
        provider_request_id="fixture-old" if conflict == "amount" else "fixture-new",
    )
    reason = "cost_settlement_conflict" if conflict == "amount" else "cost_receipt_conflict"

    async def rejected() -> None:
        with pytest.raises(BudgetDenied, match=reason):
            await budget.settle(permit.call_id, usage)

    try:
        async with transaction_mode(storage, backend):
            await during_commit(
                storage.database,
                lambda session: session.execute(
                    update(ModelCostRecord)
                    .where(ModelCostRecord.call_id == permit.call_id)
                    .values(state="estimated", charged_micros=20, provider_request_id="fixture-old")
                ),
                rejected,
            )
            async with storage.database.sessions() as session:
                row = await session.get_one(ModelCostRecord, permit.call_id)
                assert (
                    row.state == "estimated"
                    and row.charged_micros == 20
                    and row.provider_request_id == "fixture-old"
                )
                assert await session.get(TaskRunRecord, budget.run_id) is None
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
async def test_conflicting_late_fee_settlements_have_one_accepted_receipt(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, budget, permit = await prepared(backend, tmp_path)
    scalar = AsyncSession.scalar
    readers = 0
    both = asyncio.Event()

    async def simultaneous_read(
        session: AsyncSession, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal readers
        row = await scalar(session, statement, *args, **kwargs)
        # SQLite ignores SELECT FOR UPDATE. Force two stale ledger reads;
        # an actual first-write fence cannot enter this vulnerable read path.
        if (
            backend.startswith("sqlite")
            and isinstance(statement, Select)
            and any(item.get("entity") is ModelCostRecord for item in statement.column_descriptions)
        ):
            readers += 1
            if readers == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 3)
        return row

    monkeypatch.setattr(AsyncSession, "scalar", simultaneous_read)
    try:
        async with transaction_mode(storage, backend):
            outcomes = await asyncio.gather(
                *(
                    budget.settle(
                        permit.call_id,
                        ModelUsage(
                            input_tokens=tokens,
                            total_tokens=tokens,
                            usage_known=True,
                            provider_request_id=f"fixture-{tokens}",
                        ),
                    )
                    for tokens in (20, 30)
                ),
                return_exceptions=True,
            )
            assert sum(item is None for item in outcomes) == 1
            rejections = [item for item in outcomes if isinstance(item, BudgetDenied)]
            assert len(rejections) == 1 and rejections[0].reason_code == "cost_receipt_conflict"
            # Use execute here to keep the test's forced SELECT barrier local
            # to settlement rather than this final evidence inspection.
            async with storage.database.sessions() as session:
                row = (
                    await session.execute(
                        select(ModelCostRecord).where(ModelCostRecord.call_id == permit.call_id)
                    )
                ).scalar_one()
                assert row.state == "estimated" and row.charged_micros in {20, 30}
                assert row.provider_request_id == f"fixture-{row.charged_micros}"
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("known", [False, True])
async def test_live_run_settlement_uses_writes_and_preserves_unknown_hold(
    backend: str, known: bool, tmp_path: Path
) -> None:
    from sqlalchemy import event

    from app.db import ModelReservationRecord

    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "settle.db", url)
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
        budget = await make_budget(storage.database, RunBudgetConfig())
        permit = await budget.reserve(
            endpoint="fixture",
            tokens=100,
            final=True,
            pricing=ModelPricing(input_rate=1, output_rate=2, currency="CNY"),
        )
        event.listen(storage.database.engine.sync_engine, "before_cursor_execute", before_execute)
        installed = True
        await budget.settle(
            permit.call_id,
            ModelUsage(input_tokens=20, output_tokens=10, total_tokens=30, usage_known=True)
            if known
            else None,
        )
        event.remove(storage.database.engine.sync_engine, "before_cursor_execute", before_execute)
        installed = False
        assert len(statements) == (5 if known else 4)
        assert all(statement.lstrip().upper().startswith("UPDATE ") for statement in statements)
        async with storage.database.sessions() as session:
            root = await session.get_one(TaskRunRecord, budget.run_id)
            record = await session.get_one(ModelReservationRecord, permit.call_id)
            fee = await session.get_one(ModelCostRecord, permit.call_id)
            assert root.budget_tokens == (30 if known else 100) and root.llm_attempts == 1
            assert record.state == ("settled" if known else "unknown")
            assert fee.state == ("estimated" if known else "unknown")
            assert fee.charged_micros == (40 if known else 200)
    finally:
        if installed:
            event.remove(
                storage.database.engine.sync_engine, "before_cursor_execute", before_execute
            )
        await storage.close()


async def test_postgres_late_settlement_matches_recovery_run_then_fee_lock_order(
    tmp_path: Path,
) -> None:
    from sqlalchemy import event

    from app.db import ModelReservationRecord

    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "ordering.db", url)
    attempted = asyncio.Event()
    task: asyncio.Task[None] | None = None
    installed = False
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        permit = await budget.reserve(
            endpoint="fixture",
            tokens=100,
            final=True,
            pricing=ModelPricing(input_rate=1, output_rate=2, currency="CNY"),
        )

        def before_execute(
            connection: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            if (
                statement.lstrip().upper().startswith("UPDATE ")
                and "task_run" in statement
                and any(budget.run_id in values.values() for values in context.compiled_parameters)
            ):
                attempted.set()

        async with storage.database.sessions.begin() as recovery:
            await recovery.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == budget.run_id)
                .values(status="failed")
            )
            event.listen(
                storage.database.engine.sync_engine, "before_cursor_execute", before_execute
            )
            installed = True
            task = asyncio.create_task(
                budget.settle(
                    permit.call_id,
                    ModelUsage(
                        input_tokens=20, output_tokens=10, total_tokens=30, usage_known=True
                    ),
                )
            )
            await asyncio.wait_for(attempted.wait(), 3)
            assert not task.done()
            # Recovery owns Run before touching reservation and persistent fee.
            # Settlement must wait on Run without already holding that fee.
            await recovery.execute(
                update(ModelReservationRecord)
                .where(ModelReservationRecord.call_id == permit.call_id)
                .values(state="unknown")
            )
            await asyncio.wait_for(
                recovery.execute(
                    update(ModelCostRecord)
                    .where(ModelCostRecord.call_id == permit.call_id)
                    .values(state="unknown")
                ),
                3,
            )
        await asyncio.wait_for(task, 3)
        async with storage.database.sessions() as session:
            root = await session.get_one(TaskRunRecord, budget.run_id)
            record = await session.get_one(ModelReservationRecord, permit.call_id)
            fee = await session.get_one(ModelCostRecord, permit.call_id)
            assert root.status == "failed" and root.budget_tokens == 30
            assert (
                record.state == "settled" and fee.state == "estimated" and fee.charged_micros == 40
            )
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
