"""Model calls inside confirmed plans retain their original tool-budget source."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from test_database_config import config_yaml
from test_llm import FakeProvider
from test_operation_authority_fence import prepared
from test_resource_budget import Args, seed

from app.cognition import ActionInvocation, ActionPlanService, ActionStepView
from app.cognition.action_registry import ActionDefinition, ActionRegistry
from app.config import ConfigSnapshot, ConfigStore
from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, ModelCostRecord, ModelReservationRecord, TaskRunRecord
from app.harness.budget import (
    BudgetDenied,
    budget_scope,
    current_budget,
    current_tool_budget,
    tool_budget_scope,
)
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult, LLMMessage, LLMRoute, LLMRouter
from app.runs.budget import RunModelBudget
from app.runs.completion import complete_with_run
from app.runs.resources import RunToolBudget, plan_tool_budget
from app.schemas import PrivacyLevel
from app.tools import ToolResult
from scripts.benchmark_storage import FixtureStorage


async def setup(
    backend: str,
    tmp_path: Path,
    *,
    enabled: bool = True,
    budget_yaml: str = "",
    priced: bool = False,
) -> tuple[FixtureStorage, UUID, UUID, ConfigSnapshot, FakeProvider, LLMRouter]:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        path = tmp_path / "plan-model.yaml"
        yaml = config_yaml()
        if priced:
            yaml = yaml.replace(
                "output_cost_per_million: 0",
                "output_cost_per_million: 1\n    cost_currency: CNY",
            )
        path.write_text(yaml + f"\nrun_budget:\n  enabled: {str(enabled).lower()}\n" + budget_yaml)
        snapshot = await ConfigStore(path).load()
        provider = FakeProvider("cloud")
        router = LLMRouter(
            endpoints=snapshot.config.models,
            routes=snapshot.config.routes,
            providers={"cloud": provider, "local": FakeProvider("local")},
        )
        return storage, owner, root, snapshot, provider, router
    except BaseException:
        await storage.close()
        raise


def request() -> CompletionRequest:
    return CompletionRequest(
        trace_id=uuid7(),
        privacy_level=PrivacyLevel.L1,
        route=LLMRoute.UTILITY,
        max_tokens=128,
        messages=[LLMMessage(role="user", content="synthetic plan model request")],
    )


async def complete(
    storage: FixtureStorage,
    snapshot: ConfigSnapshot,
    owner: UUID,
    router: LLMRouter,
) -> CompletionResult:
    accepted = request()
    return await complete_with_run(
        storage.database,
        snapshot,
        accepted,
        router.complete,
        user_id=owner,
        kind="fixture.plan.model",
        source_id=accepted.trace_id,
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_confirmed_plan_model_uses_original_root_after_delivery_deadline(
    backend: str,
    enabled: bool,
    tmp_path: Path,
) -> None:
    storage, owner, source, snapshot, provider, router = await setup(
        backend, tmp_path, enabled=enabled
    )
    try:
        registry = ActionRegistry()
        registry.register(
            ActionDefinition(
                action_id="fixture.model",
                label="Fixture",
                description="Fixture",
                risk="A2",
                confirmation_policy="always",
                tool_name="fixture_model",
                arguments_schema=Args.model_json_schema(),
            ),
            Args,
        )

        async def runner(step: ActionStepView, actual_owner: UUID) -> ToolResult:
            assert current_budget() is None
            original = current_tool_budget()
            assert isinstance(original, RunToolBudget) and original.run_id == source
            assert (await complete(storage, snapshot, actual_owner, router)).text == "ok"
            assert current_budget() is None and current_tool_budget() is original
            return ToolResult(ok=True, tool_name=step.tool_name, latency_ms=0)

        plans = ActionPlanService(storage.database, registry, runner=runner)
        plans.set_tool_budget_builder(
            lambda root, user, deadline: plan_tool_budget(
                storage.database, root, user, deadline, snapshot.config.run_budget
            )
        )
        plan = await plans.create_plan(
            user_id=owner,
            source_turn_id=source,
            invocations=[
                ActionInvocation(action_id="fixture.model", arguments={"value": "synthetic"})
            ],
        )
        confirmation_id = uuid7()
        now = datetime.now(UTC)
        async with storage.database.sessions.begin() as sql:
            original = await sql.get_one(TaskRunRecord, source)
            original.status = "succeeded"
            original.deadline = now - timedelta(seconds=1)
            sql.add(
                TaskRunRecord(
                    id=confirmation_id,
                    user_id=owner,
                    status="running",
                    privacy_level="L1",
                    contract={"criterion": "reply_committed", "required_work": []},
                    budget=RunBudgetConfig().model_dump(mode="json"),
                    deadline=now + timedelta(minutes=1),
                    created_at=now,
                    updated_at=now,
                )
            )
        confirmation = RunModelBudget(
            storage.database, run_id=confirmation_id, user_id=owner, config=RunBudgetConfig()
        )
        with budget_scope(confirmation):
            await plans.confirm_plan(user_id=owner, plan_id=plan.id)
            result = await plans.execute_plan(user_id=owner, plan_id=plan.id)
            assert result.status == "completed"
            assert current_budget() is confirmation
        async with storage.database.sessions() as sql:
            original = await sql.get_one(TaskRunRecord, source)
            later = await sql.get_one(TaskRunRecord, confirmation_id)
            runs = list(await sql.scalars(select(TaskRunRecord)))
            reservations = list(await sql.scalars(select(ModelReservationRecord)))
        assert original.status == "succeeded" and original.llm_attempts == 1
        assert later.status == "running" and later.llm_attempts == 0
        assert {row.id for row in runs} == {source, confirmation_id}
        assert len(provider.requests) == len(reservations) == 1
        assert reservations[0].run_id == source
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "change",
    [
        "attempts",
        "tokens",
        "cancel",
        "owner",
        "expired",
        "active",
        "both_stricter",
        "different_root",
        "privacy",
    ],
)
async def test_tool_parent_model_cannot_obtain_new_authority(
    backend: str,
    change: str,
    tmp_path: Path,
) -> None:
    storage, owner, root, snapshot, provider, router = await setup(backend, tmp_path)
    try:
        config = RunBudgetConfig(max_llm_attempts=2)
        now = datetime.now(UTC)
        allowed_runs = {root}
        async with storage.database.sessions.begin() as sql:
            original = await sql.get_one(TaskRunRecord, root)
            original.status = "running" if change in {"active", "both_stricter"} else "succeeded"
            original.budget = config.model_dump(mode="json")
            if change == "attempts":
                original.llm_attempts = config.max_llm_attempts
            elif change == "tokens":
                original.budget_tokens = config.max_tokens
            elif change == "cancel":
                original.contract = {**original.contract, "work_cancel_requested": True}
            elif change == "owner":
                user = await sql.get_one(AppUserRecord, owner)
                user.status = "inactive"
            elif change == "privacy":
                original.privacy_level = "L2"
        tool = RunToolBudget(
            storage.database,
            run_id=root,
            user_id=owner,
            config=config,
            maintenance=True,
            deadline=now + timedelta(seconds=-1 if change == "expired" else 30),
            allow_active_model_parent=False,
        )
        model = None
        if change == "both_stricter":
            model = RunModelBudget(
                storage.database,
                run_id=root,
                user_id=owner,
                config=config,
                phase="maintenance",
                allow_active_parent=True,
                delivery_deadline=tool.delivery_deadline,
            )
        elif change == "different_root":
            other_root = uuid7()
            allowed_runs.add(other_root)
            async with storage.database.sessions.begin() as sql:
                sql.add(
                    TaskRunRecord(
                        id=other_root,
                        user_id=owner,
                        status="succeeded",
                        privacy_level="L1",
                        contract={"criterion": "reply_committed", "required_work": []},
                        budget=config.model_dump(mode="json"),
                        deadline=now + timedelta(minutes=1),
                        created_at=now,
                        updated_at=now,
                    )
                )
            model = RunModelBudget(
                storage.database,
                run_id=other_root,
                user_id=owner,
                config=config,
                phase="maintenance",
                delivery_deadline=tool.delivery_deadline,
            )
        with budget_scope(model), tool_budget_scope(tool), pytest.raises(BudgetDenied):
            await complete(storage, snapshot, owner, router)
        assert not provider.requests
        async with storage.database.sessions() as sql:
            assert set(await sql.scalars(select(TaskRunRecord.id))) == allowed_runs
            assert not list(await sql.scalars(select(ModelReservationRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("fail", [False, True])
async def test_inherited_model_failure_retains_original_reservation(
    backend: str,
    fail: bool,
    tmp_path: Path,
) -> None:
    storage, owner, root, snapshot, provider, router = await setup(backend, tmp_path)
    try:
        config = RunBudgetConfig(max_llm_attempts=2)
        async with storage.database.sessions.begin() as sql:
            original = await sql.get_one(TaskRunRecord, root)
            original.status = "succeeded"
            original.budget = config.model_dump(mode="json")
        provider.fail = fail
        tool = RunToolBudget(
            storage.database,
            run_id=root,
            user_id=owner,
            config=config,
            maintenance=True,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
        with budget_scope(None), tool_budget_scope(tool):
            if fail:
                from app.llm import LLMRouteExhausted

                with pytest.raises(LLMRouteExhausted):
                    await complete(storage, snapshot, owner, router)
            else:
                assert (await complete(storage, snapshot, owner, router)).text == "ok"
            assert current_budget() is None and current_tool_budget() is tool
        async with storage.database.sessions() as sql:
            original = await sql.get_one(TaskRunRecord, root)
            runs = list(await sql.scalars(select(TaskRunRecord)))
            reservations = list(await sql.scalars(select(ModelReservationRecord)))
        attempts = 2 if fail else 1
        assert len(runs) == 1 and original.status == "succeeded"
        assert original.llm_attempts == attempts
        assert len(provider.requests) == len(reservations) == attempts
        assert all(row.run_id == root and row.state == "unknown" for row in reservations)
        assert original.budget_tokens > 0
        if fail:
            with budget_scope(None), tool_budget_scope(tool), pytest.raises(BudgetDenied):
                await complete(storage, snapshot, owner, router)
            assert len(provider.requests) == attempts
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["attempts", "tokens", "money", "currency"])
async def test_inherited_model_obeys_current_stricter_limits(
    backend: str, change: str, tmp_path: Path
) -> None:
    fields = {
        "attempts": "  max_llm_attempts: 2\n",
        "tokens": "  max_tokens: 1024\n",
        "money": "  max_daily_cost: 0\n  cost_currency: CNY\n",
        "currency": "  max_daily_cost: 1\n  cost_currency: USD\n",
    }
    storage, owner, root, snapshot, provider, router = await setup(
        backend, tmp_path, budget_yaml=fields[change], priced=True
    )
    try:
        config = RunBudgetConfig(
            max_llm_attempts=4, max_tokens=8192, max_daily_cost=1, cost_currency="CNY"
        )
        async with storage.database.sessions.begin() as sql:
            original = await sql.get_one(TaskRunRecord, root)
            original.status = "succeeded"
            original.budget = config.model_dump(mode="json")
            original.llm_attempts = 2 if change == "attempts" else 0
            original.budget_tokens = 1023 if change == "tokens" else 0
        tool = RunToolBudget(
            storage.database,
            run_id=root,
            user_id=owner,
            config=config,
            maintenance=True,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
        with budget_scope(None), tool_budget_scope(tool), pytest.raises(BudgetDenied):
            await complete(storage, snapshot, owner, router)
        assert not provider.requests
        async with storage.database.sessions() as sql:
            assert list(await sql.scalars(select(TaskRunRecord.id))) == [root]
            assert not list(await sql.scalars(select(ModelReservationRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))
    finally:
        await storage.close()
