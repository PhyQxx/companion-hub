"""Completed roots can fund bounded maintenance without reopening themselves."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, update
from test_voice_parent_budget import parent_budget
from test_voice_turn_delivery import fixture
from test_voice_websocket import FakeRecognizer

from app.db import ModelCostRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, budget_scope
from app.harness.operations import OperationPolicy
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.runs.budget import RunModelBudget
from app.runs.operation import operate_with_run


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("mode", ["voice", "operation"])
@pytest.mark.parametrize("deadline", ["expired", "absent"])
async def test_completed_parent_uses_finite_maintenance_scope(
    backend: str, mode: str, deadline: str, tmp_path: Path
) -> None:
    recognizer = FakeRecognizer("synthetic utterance")
    storage, manager, session, service, _, provider, store = await fixture(
        backend, tmp_path, recognizer
    )
    try:
        original = await parent_budget(storage, session)
        async with storage.database.sessions.begin() as sql:
            await sql.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == original.run_id)
                .values(
                    status="succeeded",
                    deadline=datetime.now(UTC) - timedelta(seconds=1)
                    if deadline == "expired"
                    else None,
                )
            )
        expiry = datetime.now(UTC) + timedelta(seconds=30)
        parent = RunModelBudget(
            storage.database,
            run_id=original.run_id,
            user_id=original.owner_id,
            config=original.budget_config,
            phase="maintenance",
            delivery_deadline=expiry,
        )
        with budget_scope(parent):
            if mode == "voice":
                _, chain = await manager._voice_source.resolve()
                await manager._run_utterance(session, b"\x01\x00" * 16000, recognizer, chain)
            else:

                async def guard() -> None:
                    await manager._validate_source(session, manager._source_claim(session))

                async def invoke(start: Callable[[], Awaitable[None]]) -> bool:
                    await start()
                    return True

                config = store.current
                policy = OperationPolicy(
                    config.version,
                    tuple(config.config.run_budget.model_dump().items()),
                    UnitCostQuote(
                        pricing=UnitPricing(
                            unit="request", currency="CNY", rate_per_unit=Decimal(".002")
                        ),
                        maximum_quantity=1,
                    ),
                )
                assert await operate_with_run(
                    storage.database,
                    policy,
                    user_id=parent.owner_id,
                    privacy_level=session.privacy_level,
                    entry="synthetic.maintenance",
                    invoke=invoke,
                    evidence=lambda _: {"synthetic_result": "returned"},
                    source_guard=guard,
                    cost_endpoint="synthetic.maintenance",
                )
        async with storage.database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            root = next(row for row in runs if row.id == parent.run_id)
            assert root.status == "succeeded"
            children = [row for row in runs if row.id != parent.run_id]
            assert children and all(row.status == "succeeded" for row in children)
            assert all(
                row.deadline is not None and row.deadline.replace(tzinfo=UTC) <= expiry
                for row in children
            )
            costs = list(
                await sql.scalars(select(ModelCostRecord).where(ModelCostRecord.unit == "request"))
            )
            assert len(costs) == (2 if mode == "voice" else 1)
            assert all(row.state == "unknown" for row in costs)
            assert root.llm_attempts == (1 if mode == "voice" else 0)
        assert len(provider.requests) == (1 if mode == "voice" else 0)
    finally:
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("mode", ["voice", "operation"])
@pytest.mark.parametrize(
    "case",
    [
        "interactive_completed",
        "maintenance_cancelled",
        "maintenance_failed",
        "maintenance_expired",
        "budget_disabled",
        "budget_missing",
    ],
)
async def test_maintenance_cannot_revive_invalid_parent(
    backend: str, mode: str, case: str, tmp_path: Path
) -> None:
    recognizer = FakeRecognizer("synthetic utterance")
    storage, manager, session, service, _, provider, store = await fixture(
        backend, tmp_path, recognizer
    )
    calls = []
    try:
        original = await parent_budget(storage, session)
        status = (
            "cancelled"
            if case == "maintenance_cancelled"
            else "failed"
            if case == "maintenance_failed"
            else "succeeded"
        )
        async with storage.database.sessions.begin() as sql:
            row = await sql.get_one(TaskRunRecord, original.run_id)
            row.status = status
            if case == "budget_disabled":
                row.budget = {**(row.budget or {}), "enabled": False}
            elif case == "budget_missing":
                row.budget = None
        parent = RunModelBudget(
            storage.database,
            run_id=original.run_id,
            user_id=original.owner_id,
            config=original.budget_config,
            phase="interactive" if case == "interactive_completed" else "maintenance",
            delivery_deadline=datetime.now(UTC)
            + timedelta(seconds=-1 if case == "maintenance_expired" else 30),
        )
        with budget_scope(parent):
            if mode == "voice":
                _, chain = await manager._voice_source.resolve()
                await manager._run_utterance(session, b"\x01\x00" * 16000, recognizer, chain)
            else:

                async def guard() -> None:
                    await manager._validate_source(session, manager._source_claim(session))

                async def invoke(start: Callable[[], Awaitable[None]]) -> bool:
                    await start()
                    calls.append(True)
                    return True

                config = store.current
                policy = OperationPolicy(
                    config.version,
                    tuple(config.config.run_budget.model_dump().items()),
                    UnitCostQuote(
                        pricing=UnitPricing(
                            unit="request", currency="CNY", rate_per_unit=Decimal(".002")
                        ),
                        maximum_quantity=1,
                    ),
                )
                with pytest.raises(BudgetDenied):
                    await operate_with_run(
                        storage.database,
                        policy,
                        user_id=parent.owner_id,
                        privacy_level=session.privacy_level,
                        entry="synthetic.maintenance",
                        invoke=invoke,
                        evidence=lambda _: {"synthetic_result": "returned"},
                        source_guard=guard,
                        cost_endpoint="synthetic.maintenance",
                    )
        assert not calls and not provider.requests and recognizer.calls == 0
        async with storage.database.sessions() as sql:
            assert (await sql.get_one(TaskRunRecord, parent.run_id)).status == status
            assert not list(await sql.scalars(select(ModelCostRecord)))
    finally:
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_maintenance_operation_expiry_joins_provider_and_keeps_fee(
    backend: str, tmp_path: Path
) -> None:
    storage, manager, session, service, _, _, store = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), tts=False
    )
    entered, closed = asyncio.Event(), asyncio.Event()
    task = None
    try:
        original = await parent_budget(storage, session)
        async with storage.database.sessions.begin() as sql:
            await sql.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == original.run_id)
                .values(status="succeeded", deadline=None)
            )
        expiry = datetime.now(UTC) + timedelta(seconds=2)
        parent = RunModelBudget(
            storage.database,
            run_id=original.run_id,
            user_id=original.owner_id,
            config=original.budget_config,
            phase="maintenance",
            delivery_deadline=expiry,
        )

        async def guard() -> None:
            await manager._validate_source(session, manager._source_claim(session))

        async def invoke(start: Callable[[], Awaitable[None]]) -> bool:
            await start()
            entered.set()
            try:
                await asyncio.Event().wait()
                return True
            finally:
                closed.set()

        config = store.current
        policy = OperationPolicy(
            config.version,
            tuple(config.config.run_budget.model_dump().items()),
            UnitCostQuote(
                pricing=UnitPricing(unit="request", currency="CNY", rate_per_unit=Decimal(".002")),
                maximum_quantity=1,
            ),
        )
        with budget_scope(parent):
            task = asyncio.create_task(
                operate_with_run(
                    storage.database,
                    policy,
                    user_id=parent.owner_id,
                    privacy_level=session.privacy_level,
                    entry="synthetic.maintenance",
                    invoke=invoke,
                    evidence=lambda _: {"synthetic_result": "returned"},
                    source_guard=guard,
                    cost_endpoint="synthetic.maintenance",
                )
            )
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises((BudgetDenied, TimeoutError)):
            await asyncio.wait_for(task, 4)
        assert closed.is_set()
        async with storage.database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            root = next(row for row in runs if row.id == parent.run_id)
            leaf = next(row for row in runs if row.id != parent.run_id)
            assert root.status == "succeeded" and leaf.status in {"failed", "cancelled"}
            assert leaf.deadline is not None and leaf.deadline.replace(tzinfo=UTC) == expiry
            fee = (await sql.scalars(select(ModelCostRecord))).one()
            assert fee.state == "unknown" and fee.charged_micros == 2000
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.drain_background_work()
        await storage.close()
