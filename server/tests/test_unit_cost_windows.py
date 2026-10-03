"""Unit reservations and child operations retain parent caps and UTC admission windows."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_resource_budget import seed
from test_run_cancel_fence import prepared

from app.config.models import RunBudgetConfig
from app.db import ModelCostRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, budget_scope
from app.harness.operations import OperationPolicy
from app.harness.time import utc
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.ids import uuid7
from app.runs import operation, unit_costs
from app.runs.budget import RunModelBudget
from app.runs.costs import check_cost_allowance
from app.runs.unit_costs import UnitCostStore
from app.schemas import PrivacyLevel

BEFORE = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)
AFTER = datetime(2026, 11, 1, tzinfo=UTC)
QUOTE = UnitCostQuote(
    pricing=UnitPricing(unit="request", currency="CNY", rate_per_unit=Decimal("0.001")),
    maximum_quantity=Decimal(1),
)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_timestamp_is_sampled_after_owned_ledger_lock(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _ = await seed(storage.database)
        now = BEFORE
        original = unit_costs.lock_unit_cost

        async def locked(session: AsyncSession, **kwargs: Any) -> ModelCostRecord | None:
            nonlocal now
            result = await original(session, **kwargs)
            now = AFTER
            return result

        monkeypatch.setattr(unit_costs, "lock_unit_cost", locked)
        store, call = UnitCostStore(storage.database, clock=lambda: now), uuid7()
        await store.reserve(
            call_id=call,
            user_id=owner,
            endpoint="synthetic",
            quote=QUOTE,
            config=RunBudgetConfig(cost_currency="CNY", max_daily_cost=1),
        )
        async with storage.database.sessions() as session:
            row = await session.get_one(ModelCostRecord, call)
            assert utc(row.created_at) == AFTER
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("period", ["day", "month"])
async def test_period_change_after_allowance_rolls_back_whole_reservation(
    backend: str,
    period: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _ = await seed(storage.database)
        now = BEFORE
        original = check_cost_allowance

        async def checked(session: AsyncSession, **kwargs: Any) -> None:
            nonlocal now
            await original(session, **kwargs)
            now = AFTER

        monkeypatch.setattr(unit_costs, "check_cost_allowance", checked)
        config = RunBudgetConfig(
            cost_currency="CNY",
            max_daily_cost=1 if period == "day" else None,
            max_monthly_cost=1 if period == "month" else None,
        )
        store, call = UnitCostStore(storage.database, clock=lambda: now), uuid7()
        with pytest.raises(BudgetDenied, match="cost_window_changed"):
            await store.reserve(
                call_id=call, user_id=owner, endpoint="synthetic", quote=QUOTE, config=config
            )
        async with storage.database.sessions() as session:
            assert await session.get(ModelCostRecord, call) is None
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("priced", [False, True])
async def test_child_cannot_bypass_parent_budget_object_stricter_than_persistent_snapshot(
    backend: str,
    priced: bool,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        config = RunBudgetConfig()
        parent = RunModelBudget(
            storage.database,
            run_id=root,
            user_id=owner,
            config=RunBudgetConfig(cost_currency="CNY", max_daily_cost=0),
        )
        calls = 0

        async def guard() -> None:
            return None

        async def provider(start: Any) -> str:
            nonlocal calls
            await start()
            calls += 1
            return "synthetic"

        with (
            budget_scope(parent),
            pytest.raises(
                BudgetDenied,
                match="daily_cost_budget_exhausted"
                if priced
                else "media_cost_estimate_unavailable",
            ),
        ):
            await operation.operate_with_run(
                storage.database,
                OperationPolicy(
                    1, tuple(config.model_dump(mode="json").items()), QUOTE if priced else None
                ),
                user_id=owner,
                privacy_level=PrivacyLevel.L1,
                entry="synthetic.unit",
                invoke=provider,
                evidence=lambda _: {},
                source_guard=guard,
                cost_endpoint="synthetic",
            )
        assert calls == 0
        async with storage.database.sessions() as session:
            assert list(await session.scalars(select(ModelCostRecord))) == []
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("started", [False, True])
async def test_expired_operation_releases_only_reserved_unit_fees(
    backend: str,
    started: bool,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        store = UnitCostStore(storage.database)
        await store.reserve(
            call_id=root, user_id=owner, endpoint="synthetic", quote=QUOTE, config=RunBudgetConfig()
        )
        if started:
            await store.mark_started(call_id=root, user_id=owner)
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == root)
                .values(
                    deadline=datetime.now(UTC) - timedelta(seconds=1),
                    contract={
                        "criterion": "provider_response_returned",
                        "operation_state": "started" if started else "not_started",
                    },
                )
            )
        assert await operation.recover_expired_operations(storage.database) == 1
        assert await operation.recover_expired_operations(storage.database) == 0
        async with storage.database.sessions() as session:
            row = await session.get_one(ModelCostRecord, root)
            assert (row.state, row.charged_micros) == (
                ("unknown", 1000) if started else ("estimated", 0)
            )
            assert (await session.get_one(TaskRunRecord, root)).status == "failed"
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_cross_period_commit_never_returns_unit_admission_and_releases_only_unstarted(
    backend: str,
    tmp_path: Path,
) -> None:
    from sqlalchemy import event
    from sqlalchemy.engine import Connection

    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _ = await seed(storage.database)
        now, inserted = BEFORE, False
        store, call = UnitCostStore(storage.database, clock=lambda: now), uuid7()

        def issued(
            _connection: Connection,
            _cursor: Any,
            _statement: str,
            _parameters: Any,
            context: Any,
            _executemany: bool,
        ) -> None:
            nonlocal inserted
            statement = context.compiled.statement if context.compiled is not None else None
            if (
                statement is not None
                and statement.is_insert
                and statement.table.name == "model_cost"
            ):
                inserted = True

        def commit(_connection: Connection) -> None:
            nonlocal now
            if inserted:
                now = AFTER

        engine = storage.database.engine.sync_engine
        event.listen(engine, "after_cursor_execute", issued)
        event.listen(engine, "commit", commit)
        try:
            with pytest.raises(BudgetDenied, match="cost_window_changed"):
                await store.reserve(
                    call_id=call,
                    user_id=owner,
                    endpoint="synthetic",
                    quote=QUOTE,
                    config=RunBudgetConfig(cost_currency="CNY", max_daily_cost=1),
                )
        finally:
            event.remove(engine, "after_cursor_execute", issued)
            event.remove(engine, "commit", commit)
        async with storage.database.sessions() as session:
            row = await session.get_one(ModelCostRecord, call)
            assert (row.state, row.charged_micros, row.unit_quantity) == (
                "estimated",
                0,
                Decimal(0),
            )
    finally:
        await storage.close()


async def test_explicit_sqlite_expiry_recovery_fences_before_reading_current_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from sqlalchemy.sql import Select
    from test_delivery_sqlite_transactions import explicit_transactions

    storage = await prepared("sqlite", tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        store = UnitCostStore(storage.database)
        await store.reserve(
            call_id=root, user_id=owner, endpoint="synthetic", quote=QUOTE, config=RunBudgetConfig()
        )
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == root)
                .values(
                    deadline=datetime.now(UTC) - timedelta(seconds=1),
                    contract={
                        "criterion": "provider_response_returned",
                        "operation_state": "not_started",
                    },
                )
            )
        original = AsyncSession.execute
        read_count, both_read = 0, asyncio.Event()

        async def read_barrier(
            session: AsyncSession, statement: Any, *args: Any, **kwargs: Any
        ) -> Any:
            nonlocal read_count
            result = await original(session, statement, *args, **kwargs)
            if (
                isinstance(statement, Select)
                and getattr(statement._limit_clause, "value", None) == 50
                and statement.column_descriptions[0].get("entity") is TaskRunRecord
            ):
                read_count += 1
                if read_count == 2:
                    both_read.set()
                await asyncio.wait_for(both_read.wait(), 3)
            return result

        monkeypatch.setattr(AsyncSession, "execute", read_barrier)
        async with explicit_transactions(storage.database):
            results = await asyncio.gather(
                *(operation.recover_expired_operations(storage.database) for _ in range(2)),
                return_exceptions=True,
            )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        assert read_count == 2
        assert sorted(result for result in results if isinstance(result, int)) == [0, 1]
        async with storage.database.sessions() as session:
            row = await session.get_one(ModelCostRecord, root)
            assert row.state == "estimated" and row.charged_micros == 0
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("stage", ["accept", "start"])
async def test_media_commit_window_gate_never_dispatches_or_keeps_unissued_fee(
    backend: str,
    stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import tzinfo

    from sqlalchemy import event
    from sqlalchemy.engine import Connection

    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _ = await seed(storage.database)
        now, inserted, commits, calls = BEFORE, False, 0, 0

        class Clock(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> "Clock":
                return cls.fromtimestamp(now.timestamp(), tz)

        monkeypatch.setattr(operation, "datetime", Clock)

        def issued(
            _connection: Connection,
            _cursor: Any,
            _statement: str,
            _parameters: Any,
            context: Any,
            _executemany: bool,
        ) -> None:
            nonlocal inserted
            statement = context.compiled.statement if context.compiled is not None else None
            if (
                statement is not None
                and statement.is_insert
                and statement.table.name == "model_cost"
            ):
                inserted = True

        def commit(_connection: Connection) -> None:
            nonlocal now, commits
            if inserted:
                commits += 1
                if commits == (1 if stage == "accept" else 2):
                    now = AFTER

        async def guard() -> None:
            return None

        async def provider(start: Any) -> str:
            nonlocal calls
            await start()
            calls += 1
            return "synthetic"

        engine = storage.database.engine.sync_engine
        event.listen(engine, "after_cursor_execute", issued)
        event.listen(engine, "commit", commit)
        config = RunBudgetConfig(cost_currency="CNY", max_daily_cost=1)
        try:
            with pytest.raises(BudgetDenied, match="cost_window_changed"):
                await operation.operate_with_run(
                    storage.database,
                    OperationPolicy(1, tuple(config.model_dump(mode="json").items()), QUOTE),
                    user_id=owner,
                    privacy_level=PrivacyLevel.L1,
                    entry="synthetic.window",
                    invoke=provider,
                    evidence=lambda _: {},
                    source_guard=guard,
                    cost_endpoint="synthetic",
                )
        finally:
            event.remove(engine, "after_cursor_execute", issued)
            event.remove(engine, "commit", commit)
        assert calls == 0
        async with storage.database.sessions() as session:
            cost = await session.scalar(select(ModelCostRecord))
            assert cost is not None and cost.state == "estimated" and cost.charged_micros == 0
            run = await session.get_one(TaskRunRecord, cost.call_id)
            assert run.status == "failed" and run.contract["operation_state"] == "not_started"
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_expiry_does_not_refund_unknown_fee_when_run_lacks_start_marker(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        store = UnitCostStore(storage.database)
        await store.reserve(
            call_id=root, user_id=owner, endpoint="synthetic", quote=QUOTE, config=RunBudgetConfig()
        )
        await store.mark_started(call_id=root, user_id=owner)
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == root)
                .values(
                    deadline=datetime.now(UTC) - timedelta(seconds=1),
                    contract={
                        "criterion": "provider_response_returned",
                        "operation_state": "not_started",
                    },
                )
            )
        assert await operation.recover_expired_operations(storage.database) == 1
        async with storage.database.sessions() as session:
            row = await session.get_one(ModelCostRecord, root)
            assert row.state == "unknown" and row.charged_micros == 1000
    finally:
        await storage.close()
