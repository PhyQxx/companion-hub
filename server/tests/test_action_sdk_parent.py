"""A confirmed plan's SDK work uses its source quota, not its confirmation chat."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from test_resource_budget import Args
from test_voice_parent_budget import parent_budget
from test_voice_turn_delivery import fixture
from test_voice_websocket import FakeRecognizer

from app.cognition import ActionInvocation, ActionPlanService, ActionStepView
from app.cognition.action_registry import ActionDefinition, ActionRegistry
from app.db import ModelCostRecord, TaskRunRecord
from app.harness.budget import budget_scope, current_budget, current_tool_budget
from app.harness.operations import OperationPolicy
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.runs.budget import RunModelBudget
from app.runs.operation import operate_with_run
from app.runs.resources import plan_tool_budget, resource_usage
from app.schemas import PrivacyLevel
from app.tools import ToolResult


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_owned_plan_sdk_does_not_mix_confirmation_quota(backend: str, tmp_path: Path) -> None:
    storage, _, session, service, _, _, store = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), enabled=False
    )
    try:
        source = await parent_budget(storage, session)
        confirmation = await parent_budget(storage, session)
        registry = ActionRegistry()
        registry.register(
            ActionDefinition(
                action_id="fixture.sdk",
                label="Fixture",
                description="Fixture",
                risk="A2",
                confirmation_policy="always",
                tool_name="fixture_sdk",
                arguments_schema=Args.model_json_schema(),
            ),
            Args,
        )

        async def invoke(start: Callable[[], Awaitable[None]]) -> bool:
            budget = current_budget()
            assert isinstance(budget, RunModelBudget) and budget.run_id == source.run_id
            tool = current_tool_budget()
            assert tool is not None
            permit = await tool.reserve_tool(tool_name="synthetic.sdk", user_id=source.owner_id)
            await start()
            await tool.settle_tool(permit.call_id, reported_ok=True)
            return True

        async def guard() -> None:
            return None

        async def runner(step: ActionStepView, owner: UUID) -> ToolResult:
            cfg = store.current
            assert await operate_with_run(
                storage.database,
                OperationPolicy(
                    cfg.version,
                    tuple(cfg.config.run_budget.model_dump().items()),
                    UnitCostQuote(
                        pricing=UnitPricing(unit="request", currency="CNY", rate_per_unit=".002"),
                        maximum_quantity=1,
                    ),
                ),
                user_id=owner,
                privacy_level=PrivacyLevel.L1,
                entry="synthetic.plan.sdk",
                invoke=invoke,
                evidence=lambda _: {},
                source_guard=guard,
                cost_endpoint="synthetic.plan.sdk",
            )
            return ToolResult(ok=True, tool_name=step.tool_name, latency_ms=0)

        plans = ActionPlanService(storage.database, registry, runner=runner)
        plans.set_tool_budget_builder(
            lambda run, owner, deadline: plan_tool_budget(
                storage.database, run, owner, deadline, store.current.config.run_budget
            )
        )
        plan = await plans.create_plan(
            user_id=source.owner_id,
            source_turn_id=source.run_id,
            invocations=[
                ActionInvocation(action_id="fixture.sdk", arguments={"value": "synthetic"})
            ],
        )
        async with storage.database.sessions.begin() as sql:
            original = await sql.get_one(TaskRunRecord, source.run_id)
            original.status = "succeeded"
        with budget_scope(confirmation):
            await plans.confirm_plan(user_id=source.owner_id, plan_id=plan.id)
            result = await plans.execute_plan(user_id=source.owner_id, plan_id=plan.id)
            assert result.status == "completed"
            assert current_budget() is confirmation
        async with storage.database.sessions() as sql:
            original = await sql.get_one(TaskRunRecord, source.run_id)
            later = await sql.get_one(TaskRunRecord, confirmation.run_id)
            child = await sql.scalar(
                select(TaskRunRecord).where(TaskRunRecord.parent_run_id == source.run_id)
            )
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert original.status == "succeeded" and resource_usage(original)["tool_attempts"] == 1
        assert resource_usage(later).get("tool_attempts", 0) == 0
        assert child is not None and child.status == "succeeded"
        assert len(fees) == 1 and fees[0].state == "unknown"
    finally:
        await service.drain_background_work()
        await storage.close()
