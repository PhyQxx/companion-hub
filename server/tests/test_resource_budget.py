from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, Base, Database, TaskRunEventRecord, TaskRunRecord, create_database
from app.harness.budget import BudgetDenied, budget_scope, tool_budget_scope
from app.ids import uuid7
from app.llm import ToolCall, ToolDefinition
from app.runs.budget import RunModelBudget, job_model_budget
from app.runs.resources import RunToolBudget, plan_tool_budget
from app.runs.store import RunStore
from app.tools import ToolContext, ToolExecutor, ToolRegistry, ToolResult


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    value = create_database(f"sqlite+aiosqlite:///{tmp_path / 'resources.db'}")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


async def seed(database: Database) -> tuple[UUID, UUID, RunToolBudget]:
    owner, run_id = uuid7(), uuid7()
    config = RunBudgetConfig(max_tool_attempts=1)
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add(
            AppUserRecord(id=owner, display_name="Fixture", status="active", role="owner")
        )
        session.add(
            TaskRunRecord(
                id=run_id,
                user_id=owner,
                status="running",
                privacy_level="L1",
                contract={"criterion": "reply_committed", "required_work": []},
                budget=config.model_dump(mode="json"),
                deadline=now + timedelta(minutes=5),
                created_at=now,
                updated_at=now,
            )
        )
    return owner, run_id, RunToolBudget(database, run_id=run_id, user_id=owner, config=config)


class Args(BaseModel):
    value: str


class SpyTool:
    name, description = "fixture_tool", "Fixture"
    arguments_model: type[BaseModel] = Args
    runs_local = True
    max_privacy_level = "L2"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name, description=self.description, parameters=Args.model_json_schema()
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic failure")
        # Even a handler's misleading admission claim is overwritten by Executor.
        return ToolResult(
            ok=True, admission_status="not_admitted", tool_name=self.name, latency_ms=0
        )


def call(value: str = "private synthetic body") -> ToolCall:
    return ToolCall(
        id="fixture-call", function={"name": "fixture_tool", "arguments": {"value": value}}
    )


async def test_parallel_tool_admission_is_atomic_and_settlement_is_idempotent(
    database: Database,
) -> None:
    owner, run_id, budget = await seed(database)
    results = await asyncio.gather(
        *[budget.reserve_tool(tool_name="fixture_tool", user_id=owner) for _ in range(12)],
        return_exceptions=True,
    )
    from app.harness.budget import ToolPermit

    permits = [result for result in results if isinstance(result, ToolPermit)]
    assert len(permits) == 1
    assert all(isinstance(result, BudgetDenied) for result in results if result not in permits)
    await budget.settle_tool(permits[0].call_id, reported_ok=False)
    await budget.settle_tool(permits[0].call_id, reported_ok=False)
    with pytest.raises(BudgetDenied, match="tool_settlement_conflict"):
        await budget.settle_tool(permits[0].call_id, reported_ok=True)
    view = await RunStore(database).get(run_id, user_id=owner)
    assert view.budget_summary is not None and view.budget_summary.tool_attempts == 1
    assert view.budget_summary.unsettled_tool_calls == 0
    events = await RunStore(database).events(run_id, user_id=owner)
    assert [event.kind for event in events] == ["run.tool.reserved", "run.tool.returned"]


async def test_tool_executor_denies_before_handler_and_keeps_unknown_attempt(
    database: Database,
) -> None:
    owner, run_id, budget = await seed(database)
    tool = SpyTool(fail=True)
    executor = ToolExecutor(ToolRegistry([tool]))
    context = ToolContext(user_id=owner, privacy_level="L1")
    with tool_budget_scope(budget):
        with pytest.raises(RuntimeError, match="synthetic failure"):
            await executor.execute(call(), context)
        denied = await executor.execute(call(), context)
    assert tool.calls == 1
    assert not denied.result.ok and denied.result.reason_code == "tool_budget_exhausted"
    assert denied.result.admission_status == "not_admitted"
    view = await RunStore(database).get(run_id, user_id=owner)
    assert view.budget_summary is not None and view.budget_summary.unknown_tool_calls == 1
    assert view.budget_summary.tool_attempts == 1
    assert "private synthetic body" not in str(
        await RunStore(database).events(run_id, user_id=owner)
    )


async def test_model_scope_shares_tool_quota_and_invalid_arguments_do_not_consume_it(
    database: Database,
) -> None:
    owner, run_id, _ = await seed(database)
    tool = SpyTool()
    executor = ToolExecutor(ToolRegistry([tool]))
    model = RunModelBudget(database, run_id=run_id, user_id=owner, config=RunBudgetConfig())
    context = ToolContext(user_id=owner, privacy_level="L1")
    with budget_scope(model):
        bad = await executor.execute(
            ToolCall(id="bad", function={"name": tool.name, "arguments": {}}), context
        )
        assert bad.result.reason_code == "tool_arguments_invalid"
        result = await executor.execute(call(), context)
        assert result.result.ok and result.result.admission_status == "admitted"
        assert (
            await executor.execute(call(), context)
        ).result.reason_code == "tool_budget_exhausted"
    assert tool.calls == 1


async def test_confirmed_plan_uses_source_quota_and_reports_rejected_write_as_unstarted(
    database: Database,
) -> None:
    from app.cognition import ActionInvocation, ActionPlanService, ToolActionRunner
    from app.cognition.action_registry import ActionDefinition, ActionRegistry

    owner, run_id, _ = await seed(database)
    registry = ActionRegistry()
    registry.register(
        ActionDefinition(
            action_id="fixture.write",
            label="Fixture",
            description="Fixture",
            risk="A1",
            confirmation_policy="never",
            tool_name="fixture_tool",
            arguments_schema=Args.model_json_schema(),
        ),
        Args,
    )
    tool = SpyTool()
    plans = ActionPlanService(
        database, registry, runner=ToolActionRunner(ToolExecutor(ToolRegistry([tool])))
    )
    plans.set_tool_budget_builder(
        lambda run, user, deadline: plan_tool_budget(database, run, user, deadline)
    )
    plan = await plans.create_plan(
        user_id=owner,
        source_turn_id=run_id,
        invocations=[
            ActionInvocation(action_id="fixture.write", arguments={"value": str(index)})
            for index in range(2)
        ],
    )
    result = await plans.execute_plan(user_id=owner, plan_id=plan.id)
    assert tool.calls == 1
    assert result.steps[1].reason_code == "tool_budget_exhausted"
    detail = await RunStore(database).get(run_id, user_id=owner)
    assert detail.action_outcomes[1].outcome.side_effect_state == "not_started"
    assert detail.budget_summary is not None and detail.budget_summary.tool_attempts == 1


async def test_deleted_source_cannot_be_recreated_by_late_tool_settlement(
    database: Database,
) -> None:
    owner, run_id, budget = await seed(database)
    permit = await budget.reserve_tool(tool_name="fixture_tool", user_id=owner)
    async with database.sessions.begin() as session:
        # Match the shared deletion cascade, including SQLite without FK cascades.
        await session.execute(delete(TaskRunEventRecord).where(TaskRunEventRecord.run_id == run_id))
        await session.execute(delete(TaskRunRecord).where(TaskRunRecord.id == run_id))
    await budget.settle_tool(permit.call_id, reported_ok=True)
    async with database.sessions() as session:
        assert await session.get(TaskRunRecord, run_id) is None
        assert await session.scalar(select(TaskRunEventRecord)) is None


async def test_new_background_optout_returns_no_budget_but_existing_snapshot_still_applies(
    database: Database,
) -> None:
    from app.jobs import JobEngine

    owner, _, _ = await seed(database)
    jobs = JobEngine(database)
    job = await jobs.submit(
        "deleg.test", {"user_id": str(owner)}, owner=str(owner), resource_class="deleg"
    )
    await jobs.claim("fixture", resource_class="deleg")
    assert await job_model_budget(database, job.id, RunBudgetConfig(enabled=False)) is None
    assert await job_model_budget(database, job.id, RunBudgetConfig()) is None
    second = await jobs.submit(
        "deleg.test", {"user_id": str(owner)}, owner=str(owner), resource_class="deleg"
    )
    await jobs.claim("fixture", resource_class="deleg", job_id=second.id)
    budget = await job_model_budget(database, second.id, RunBudgetConfig(max_llm_attempts=2))
    assert budget is not None
    await budget.reserve(endpoint="fixture", tokens=10, final=True)
    restored = await job_model_budget(database, second.id, RunBudgetConfig(enabled=False))
    assert restored is not None
    await restored.reserve(endpoint="fixture", tokens=10, final=True)
    with pytest.raises(BudgetDenied, match="run_budget_exhausted"):
        await restored.reserve(endpoint="fixture", tokens=10, final=True)


async def test_expired_tool_reservation_becomes_unknown_without_refund_or_replay(
    database: Database,
) -> None:
    from app.runs.resources import recover_tool_reservations

    owner, run_id, budget = await seed(database)
    permit = await budget.reserve_tool(tool_name="fixture_tool", user_id=owner)
    async with database.sessions.begin() as session:
        event = await session.scalar(
            select(TaskRunEventRecord).where(TaskRunEventRecord.run_id == run_id)
        )
        assert event is not None
        event.payload = {
            **event.payload,
            "deadline": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
    assert await recover_tool_reservations(database) == 1
    assert await recover_tool_reservations(database) == 0
    await budget.settle_tool(permit.call_id, reported_ok=None)
    detail = await RunStore(database).get(run_id, user_id=owner)
    assert detail.budget_summary is not None
    assert detail.budget_summary.tool_attempts == detail.budget_summary.unknown_tool_calls == 1
    assert detail.budget_summary.unsettled_tool_calls == 0
    with pytest.raises(BudgetDenied, match="tool_budget_exhausted"):
        await budget.reserve_tool(tool_name="fixture_tool", user_id=owner)


async def test_cancel_owner_and_deadline_checks_happen_before_tool_admission(
    database: Database,
) -> None:
    from sqlalchemy import update

    owner, run_id, budget = await seed(database)
    with pytest.raises(BudgetDenied, match="budget_owner_invalid"):
        await budget.reserve_tool(tool_name="fixture_tool", user_id=uuid7())
    await RunStore(database).cancel_work(run_id, user_id=owner)
    with pytest.raises(BudgetDenied, match="budget_run_inactive"):
        await budget.reserve_tool(tool_name="fixture_tool", user_id=owner)
    async with database.sessions.begin() as session:
        await session.execute(
            update(TaskRunRecord)
            .where(TaskRunRecord.id == run_id)
            .values(
                status="running",
                contract={"criterion": "reply_committed"},
                deadline=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    with pytest.raises(BudgetDenied, match="run_deadline_exceeded"):
        await budget.reserve_tool(tool_name="fixture_tool", user_id=owner)
    async with database.sessions() as session:
        tool_events = list(
            await session.scalars(
                select(TaskRunEventRecord).where(
                    TaskRunEventRecord.kind.like("run.tool.%"),
                )
            )
        )
        assert tool_events == []


async def test_concurrent_plan_execution_has_one_runner_on_sqlite(database: Database) -> None:
    from app.cognition import ActionInvocation, ActionPlanService, ToolActionRunner
    from app.cognition.action_registry import ActionDefinition, ActionRegistry

    owner, _, _ = await seed(database)
    registry = ActionRegistry()
    registry.register(
        ActionDefinition(
            action_id="fixture.write",
            label="Fixture",
            description="Fixture",
            risk="A1",
            confirmation_policy="never",
            tool_name="fixture_tool",
            arguments_schema=Args.model_json_schema(),
        ),
        Args,
    )
    tool = SpyTool()
    plans = ActionPlanService(
        database, registry, runner=ToolActionRunner(ToolExecutor(ToolRegistry([tool])))
    )
    plan = await plans.create_plan(
        user_id=owner,
        invocations=[
            ActionInvocation(action_id="fixture.write", arguments={"value": "synthetic"}),
        ],
    )
    results = await asyncio.gather(
        *[plans.execute_plan(user_id=owner, plan_id=plan.id) for _ in range(8)],
        return_exceptions=True,
    )
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert all(
        isinstance(result, ValueError) for result in results if isinstance(result, BaseException)
    )
    assert tool.calls == 1
