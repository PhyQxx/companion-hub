"""Disabled source quotas remain disabled across later chats and SDK children."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from test_chat_parent_budget import fixture as chat_fixture
from test_chat_parent_budget import start
from test_database_config import config_yaml
from test_llm import FakeProvider
from test_operation_authority_fence import prepared
from test_resource_budget import Args, SpyTool, call, seed
from test_voice_source_guard import invalidate, source_fixture

from app.cognition import ActionInvocation, ActionPlanService, ActionStepView
from app.cognition.action_registry import ActionDefinition, ActionRegistry
from app.config import ConfigStore
from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, ModelCostRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, budget_scope, current_budget, current_tool_budget
from app.harness.operations import OperationPolicy
from app.harness.run_trace import current_run_trace, run_trace_scope
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.runs.budget import RunModelBudget
from app.runs.completion import complete_with_run
from app.runs.operation import operate_with_run
from app.runs.resources import plan_tool_budget, resource_usage
from app.runs.trace_sources import capture_disabled_trace
from app.schemas import PrivacyLevel
from app.tools import ToolContext, ToolRegistry, ToolResult
from scripts.benchmark_storage import FixtureStorage


async def guard() -> None:
    pass


async def execute(
    storage: FixtureStorage,
    owner: UUID,
    invoke: Callable[[Callable[[], Awaitable[None]]], Awaitable[bool]],
    *,
    enabled: bool = True,
) -> bool:
    return await operate_with_run(
        storage.database,
        OperationPolicy(
            2,
            tuple(RunBudgetConfig(enabled=enabled).model_dump(mode="json").items()),
            UnitCostQuote(
                pricing=UnitPricing(unit="request", currency="CNY", rate_per_unit=".002"),
                maximum_quantity=1,
            ),
        ),
        user_id=owner,
        privacy_level=PrivacyLevel.L1,
        entry="synthetic.disabled.sdk",
        invoke=invoke,
        evidence=lambda _: {},
        source_guard=guard,
        cost_endpoint="synthetic.disabled.sdk",
        cooperative=True,
    )


async def disable(storage: FixtureStorage, root: UUID, *, succeeded: bool = False) -> None:
    async with storage.database.sessions.begin() as sql:
        row = await sql.get_one(TaskRunRecord, root)
        row.budget = RunBudgetConfig(enabled=False).model_dump(mode="json")
        row.deadline = None
        if succeeded:
            row.status = "succeeded"


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_nested_operations_keep_disabled_source_and_financial_records(
    backend: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        await disable(storage, root)
        trace = await capture_disabled_trace(storage.database, root, owner)
        assert trace is not None
        seen: list[UUID] = []

        async def inner(begin: Callable[[], Awaitable[None]]) -> bool:
            assert current_budget() is None and current_tool_budget() is None
            current = current_run_trace()
            assert current is not None and len(current.sources) == 3
            seen.append(current.run_id)
            await begin()
            return True

        async def outer(begin: Callable[[], Awaitable[None]]) -> bool:
            assert current_budget() is None and current_tool_budget() is None
            current = current_run_trace()
            assert current is not None and len(current.sources) == 2
            seen.append(current.run_id)
            await begin()
            return await execute(storage, owner, inner)

        with run_trace_scope(trace):
            assert await execute(storage, owner, outer)
            assert current_run_trace() is trace
        assert current_run_trace() is None
        async with storage.database.sessions() as sql:
            original = await sql.get_one(TaskRunRecord, root)
            children = [await sql.get_one(TaskRunRecord, identifier) for identifier in seen]
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert original.deadline is None and original.status == "running"
        assert children[0].parent_run_id == root and children[1].parent_run_id == seen[0]
        assert all(child.status == "succeeded" and child.llm_attempts == 0 for child in children)
        assert all(resource_usage(child).get("tool_attempts", 0) == 0 for child in children)
        assert len(fees) == 2 and all(fee.state == "unknown" for fee in fees)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "change", ["cancel", "owner", "privacy", "budget", "deadline", "parent", "expiry"]
)
@pytest.mark.parametrize("inflight", [False, True])
async def test_revoked_disabled_trace_cannot_accept_provider_result(
    backend: str,
    change: str,
    inflight: bool,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        await disable(storage, root)
        trace = await capture_disabled_trace(storage.database, root, owner)
        assert trace is not None
        calls = 0

        async def revoke() -> None:
            nonlocal trace
            if change == "expiry":
                assert trace is not None
                trace = replace(trace, expires_at=datetime.now(UTC) - timedelta(seconds=1))
                return
            async with storage.database.sessions.begin() as sql:
                row = await sql.get_one(TaskRunRecord, root)
                if change == "cancel":
                    row.contract = {**row.contract, "work_cancel_requested": True}
                elif change == "owner":
                    user = await sql.get_one(AppUserRecord, owner)
                    user.status = "inactive"
                elif change == "privacy":
                    row.privacy_level = "L2"
                elif change == "budget":
                    row.budget = RunBudgetConfig(enabled=True).model_dump(mode="json")
                elif change == "deadline":
                    row.deadline = datetime.now(UTC) - timedelta(seconds=1)
                else:
                    row.parent_run_id = root

        async def invoke(begin: Callable[[], Awaitable[None]]) -> bool:
            nonlocal calls
            await begin()
            calls += 1
            if change == "expiry":
                await asyncio.sleep(3.1)
            else:
                await revoke()
            return True

        if not inflight:
            await revoke()
        elif change == "expiry":
            trace = replace(trace, expires_at=datetime.now(UTC) + timedelta(seconds=3))
        with run_trace_scope(trace), pytest.raises((BudgetDenied, TimeoutError)):
            await execute(storage, owner, invoke)
        assert calls == int(inflight)
        async with storage.database.sessions() as sql:
            children = list(
                await sql.scalars(
                    select(TaskRunRecord).where(
                        TaskRunRecord.parent_run_id == root, TaskRunRecord.id != root
                    )
                )
            )
        assert all(child.status != "succeeded" for child in children)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("builder", [False, True])
async def test_disabled_plan_does_not_borrow_confirmation_budget(
    backend: str, builder: bool, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, source, _ = await seed(storage.database)
        await disable(storage, source)
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

        async def invoke(begin: Callable[[], Awaitable[None]]) -> bool:
            assert current_budget() is None and current_tool_budget() is None
            trace = current_run_trace()
            assert trace is not None and trace.sources[0].run_id == source
            await begin()
            return True

        async def runner(step: ActionStepView, actual_owner: UUID) -> ToolResult:
            assert await execute(storage, actual_owner, invoke)
            return ToolResult(ok=True, tool_name=step.tool_name, latency_ms=0)

        plans = ActionPlanService(storage.database, registry, runner=runner)
        if builder:
            plans.set_tool_budget_builder(
                lambda run, user, deadline: plan_tool_budget(storage.database, run, user, deadline)
            )
        plan = await plans.create_plan(
            user_id=owner,
            source_turn_id=source,
            invocations=[
                ActionInvocation(action_id="fixture.sdk", arguments={"value": "synthetic"})
            ],
        )
        await disable(storage, source, succeeded=True)
        confirmation_id = uuid7()
        now = datetime.now(UTC)
        async with storage.database.sessions.begin() as sql:
            sql.add(
                TaskRunRecord(
                    id=confirmation_id,
                    user_id=owner,
                    status="running",
                    privacy_level="L1",
                    contract={"criterion": "reply_committed", "required_work": []},
                    budget=RunBudgetConfig(enabled=True).model_dump(mode="json"),
                    deadline=now + timedelta(minutes=1),
                    created_at=now,
                    updated_at=now,
                )
            )
        confirmation = RunModelBudget(
            storage.database,
            run_id=confirmation_id,
            user_id=owner,
            config=RunBudgetConfig(enabled=True),
        )
        with budget_scope(confirmation):
            await plans.confirm_plan(user_id=owner, plan_id=plan.id)
            result = await plans.execute_plan(user_id=owner, plan_id=plan.id)
            assert result.status == "completed"
            assert current_budget() is confirmation and current_run_trace() is None
        async with storage.database.sessions() as sql:
            original = await sql.get_one(TaskRunRecord, source)
            later = await sql.get_one(TaskRunRecord, confirmation_id)
            children = list(
                await sql.scalars(
                    select(TaskRunRecord).where(TaskRunRecord.parent_run_id == source)
                )
            )
        assert original.status == "succeeded" and original.deadline is None
        assert resource_usage(original).get("tool_attempts", 0) == 0
        assert resource_usage(later).get("tool_attempts", 0) == 0
        assert later.llm_attempts == 0 and later.status == "running"
        assert len(children) == 1 and children[0].status == "succeeded"
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_chat_tool_keeps_original_disabled_parent(backend: str, tmp_path: Path) -> None:
    storage, service, owner, root, conversation, _ = await chat_fixture(
        backend, tmp_path, enabled=False
    )
    try:
        await disable(storage, root)
        pending = await start(service, owner, root, conversation)
        assert pending.config.run_budget.enabled is True

        class SdkTool(SpyTool):
            async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
                async def invoke(begin: Callable[[], Awaitable[None]]) -> bool:
                    assert current_budget() is None and current_tool_budget() is None
                    trace = current_run_trace()
                    assert trace is not None and trace.sources[0].run_id == root
                    await begin()
                    return True

                assert await execute(storage, owner, invoke)
                return ToolResult(ok=True, tool_name=self.name, latency_ms=0)

        registry = ToolRegistry()
        registry.register(SdkTool())
        service._device_tools = registry
        pending = replace(pending, tool_names=("fixture_tool",))
        result = await service._execute_tool_call(pending, [call()])
        assert result[0].result.ok
        async with storage.database.sessions() as sql:
            child = await sql.scalar(
                select(TaskRunRecord).where(TaskRunRecord.parent_run_id == pending.turn_id)
            )
            original = await sql.get_one(TaskRunRecord, root)
        assert child is not None and child.status == "succeeded"
        assert original.deadline is None and original.llm_attempts == 0
    finally:
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_missing_plan_budget_snapshot_is_not_opt_out(backend: str, tmp_path: Path) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        async with storage.database.sessions.begin() as sql:
            row = await sql.get_one(TaskRunRecord, root)
            row.budget = None
        with pytest.raises(BudgetDenied, match="budget_snapshot_missing"):
            await plan_tool_budget(
                storage.database, root, owner, datetime.now(UTC) + timedelta(minutes=1)
            )
        with pytest.raises(BudgetDenied, match="budget_snapshot_missing"):
            await capture_disabled_trace(storage.database, root, owner)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", [None, "cancel", "budget"])
async def test_maintenance_model_preserves_disabled_source(
    backend: str,
    change: str | None,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        await disable(storage, root, succeeded=True)
        trace = await capture_disabled_trace(storage.database, root, owner, maintenance=True)
        assert trace is not None
        path = tmp_path / "trace-model.yaml"
        path.write_text(config_yaml())
        snapshot = await ConfigStore(path).load()
        assert snapshot.config.run_budget.enabled
        request = CompletionRequest(
            trace_id=uuid7(),
            privacy_level=PrivacyLevel.L1,
            route=LLMRoute.UTILITY,
            messages=[LLMMessage(role="user", content="synthetic")],
        )
        provider = FakeProvider("cloud")

        async def complete(accepted: CompletionRequest) -> CompletionResult:
            assert current_budget() is None and current_tool_budget() is None
            child = current_run_trace()
            assert child is not None and child.sources[0].run_id == root and len(child.sources) == 2
            if change is not None:
                async with storage.database.sessions.begin() as sql:
                    original = await sql.get_one(TaskRunRecord, root)
                    if change == "cancel":
                        original.contract = {**original.contract, "work_cancel_requested": True}
                    else:
                        original.budget = RunBudgetConfig(enabled=True).model_dump(mode="json")
            return await provider.complete(accepted)

        with run_trace_scope(trace):
            if change is None:
                assert (
                    await complete_with_run(
                        storage.database,
                        snapshot,
                        request,
                        complete,
                        user_id=owner,
                        kind="fixture.trace.model",
                        source_id=request.trace_id,
                    )
                ).text == "ok"
            else:
                with pytest.raises(BudgetDenied):
                    await complete_with_run(
                        storage.database,
                        snapshot,
                        request,
                        complete,
                        user_id=owner,
                        kind="fixture.trace.model",
                        source_id=request.trace_id,
                    )
            assert current_run_trace() is trace
        async with storage.database.sessions() as sql:
            original = await sql.get_one(TaskRunRecord, root)
            child = await sql.scalar(
                select(TaskRunRecord).where(TaskRunRecord.parent_run_id == root)
            )
        assert child is not None and child.status == ("succeeded" if change is None else "failed")
        assert child.llm_attempts == original.llm_attempts == 0 and original.deadline is None
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["owner", "database"])
async def test_trace_cannot_cross_owner_or_database(
    backend: str, change: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        await disable(storage, root)
        trace = await capture_disabled_trace(storage.database, root, owner)
        assert trace is not None
        trace = (
            replace(trace, user_id=uuid7()) if change == "owner" else replace(trace, database_key=0)
        )

        async def invoke(begin: Callable[[], Awaitable[None]]) -> bool:
            pytest.fail("unbound trace must reject before any SDK")
            return False

        with run_trace_scope(trace), pytest.raises(BudgetDenied, match="budget_owner_invalid"):
            await execute(storage, owner, invoke)
        async with storage.database.sessions() as sql:
            assert list(await sql.scalars(select(TaskRunRecord.id))) == [root]
            assert not list(await sql.scalars(select(ModelCostRecord)))
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_standalone_disabled_sdk_propagates_its_owned_source(
    backend: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _ = await seed(storage.database)
        seen: list[UUID] = []

        async def inner(begin: Callable[[], Awaitable[None]]) -> bool:
            trace = current_run_trace()
            assert trace is not None and len(trace.sources) == 2
            assert current_budget() is None and current_tool_budget() is None
            seen.append(trace.run_id)
            await begin()
            return True

        async def outer(begin: Callable[[], Awaitable[None]]) -> bool:
            trace = current_run_trace()
            assert trace is not None and len(trace.sources) == 1
            seen.append(trace.run_id)
            await begin()
            return await execute(storage, owner, inner)

        assert await execute(storage, owner, outer, enabled=False)
        assert current_run_trace() is None
        async with storage.database.sessions() as sql:
            root = await sql.get_one(TaskRunRecord, seen[0])
            child = await sql.get_one(TaskRunRecord, seen[1])
        assert (
            root.parent_run_id is None
            and root.budget is not None
            and root.budget["enabled"] is False
        )
        assert child.parent_run_id == root.id and root.status == child.status == "succeeded"
        assert root.llm_attempts == child.llm_attempts == 0
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_standalone_disabled_model_propagates_its_owned_source(
    backend: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _ = await seed(storage.database)
        path = tmp_path / "trace-standalone-model.yaml"
        path.write_text(config_yaml() + "\nrun_budget:\n  enabled: false\n")
        snapshot = await ConfigStore(path).load()
        request = CompletionRequest(
            trace_id=uuid7(),
            privacy_level=PrivacyLevel.L1,
            route=LLMRoute.UTILITY,
            messages=[LLMMessage(role="user", content="synthetic")],
        )
        provider = FakeProvider("cloud")
        seen: list[UUID] = []

        async def invoke(begin: Callable[[], Awaitable[None]]) -> bool:
            trace = current_run_trace()
            assert trace is not None and len(trace.sources) == 2
            assert current_budget() is None and current_tool_budget() is None
            seen.append(trace.run_id)
            await begin()
            return True

        async def complete(accepted: CompletionRequest) -> CompletionResult:
            trace = current_run_trace()
            assert trace is not None and len(trace.sources) == 1
            seen.append(trace.run_id)
            assert await execute(storage, owner, invoke)
            return await provider.complete(accepted)

        assert (
            await complete_with_run(
                storage.database,
                snapshot,
                request,
                complete,
                user_id=owner,
                kind="fixture.standalone.model",
                source_id=request.trace_id,
            )
        ).text == "ok"
        assert current_run_trace() is None
        async with storage.database.sessions() as sql:
            root = await sql.get_one(TaskRunRecord, seen[0])
            child = await sql.get_one(TaskRunRecord, seen[1])
        assert root.parent_run_id is None and root.status == child.status == "succeeded"
        assert child.parent_run_id == root.id and root.llm_attempts == child.llm_attempts == 0
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["revoked", "expired", "archived"])
@pytest.mark.parametrize("inflight", [False, True])
async def test_disabled_voice_trace_rechecks_actual_source_actor(
    backend: str,
    change: str,
    inflight: bool,
    tmp_path: Path,
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    try:
        root = uuid7()
        now = datetime.now(UTC)
        async with storage.database.sessions.begin() as sql:
            sql.add(
                TaskRunRecord(
                    id=root,
                    user_id=source.user_id,
                    conversation_id=source.conversation_id,
                    status="running",
                    privacy_level="L1",
                    config_version=1,
                    contract={
                        "entry": "voice.utterance",
                        "source_actor": "browser",
                        "source_id": str(source.actor_id),
                    },
                    budget=RunBudgetConfig(enabled=False).model_dump(mode="json"),
                    deadline=now + timedelta(minutes=1),
                    created_at=now,
                    updated_at=now,
                )
            )
        calls = 0

        async def invoke(begin: Callable[[], Awaitable[None]]) -> bool:
            nonlocal calls
            trace = current_run_trace()
            assert trace is not None and trace.sources[0].run_id == root
            await begin()
            calls += 1
            await invalidate(storage, source, change)
            return True

        if not inflight:
            await invalidate(storage, source, change)
        with pytest.raises(BudgetDenied):
            await operate_with_run(
                storage.database,
                OperationPolicy(
                    1, tuple(RunBudgetConfig(enabled=False).model_dump(mode="json").items())
                ),
                user_id=source.user_id,
                privacy_level=PrivacyLevel.L1,
                entry="fixture.voice.sdk",
                invoke=invoke,
                evidence=lambda _: {},
                source_guard=guard,
                cost_endpoint="fixture.voice.sdk",
                trace_parent_id=root,
                cooperative=True,
            )
        assert calls == int(inflight)
        async with storage.database.sessions() as sql:
            children = list(
                await sql.scalars(select(TaskRunRecord).where(TaskRunRecord.parent_run_id == root))
            )
        assert all(child.status != "succeeded" for child in children)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_chat_cannot_replace_accepted_disabled_root(backend: str, tmp_path: Path) -> None:
    storage, service, owner, root, conversation, _ = await chat_fixture(
        backend, tmp_path, enabled=False
    )
    try:
        await disable(storage, root)
        pending = await start(service, owner, root, conversation)
        async with storage.database.sessions.begin() as sql:
            original = await sql.get_one(TaskRunRecord, root)
            original.budget = RunBudgetConfig(enabled=True).model_dump(mode="json")
            original.deadline = datetime.now(UTC) + timedelta(minutes=1)
        with pytest.raises(BudgetDenied, match="run_trace_changed"):
            await service._execute_tool_call(pending, [call()])
        async with storage.database.sessions() as sql:
            assert len(list(await sql.scalars(select(TaskRunRecord)))) == 2
            assert not list(await sql.scalars(select(ModelCostRecord)))
    finally:
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_plan_does_not_recapture_changed_source_for_next_step(
    backend: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, source, _ = await seed(storage.database)
        await disable(storage, source)
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
        calls = 0

        async def invoke(begin: Callable[[], Awaitable[None]]) -> bool:
            nonlocal calls
            calls += 1
            await begin()
            return True

        async def runner(step: ActionStepView, actual_owner: UUID) -> ToolResult:
            assert await execute(storage, actual_owner, invoke)
            async with storage.database.sessions.begin() as sql:
                original = await sql.get_one(TaskRunRecord, source)
                original.budget = RunBudgetConfig(enabled=True).model_dump(mode="json")
                original.deadline = datetime.now(UTC) + timedelta(minutes=1)
            return ToolResult(ok=True, tool_name=step.tool_name, latency_ms=0)

        plans = ActionPlanService(storage.database, registry, runner=runner)
        plans.set_tool_budget_builder(
            lambda run, user, deadline: plan_tool_budget(storage.database, run, user, deadline)
        )
        plan = await plans.create_plan(
            user_id=owner,
            source_turn_id=source,
            invocations=[
                ActionInvocation(action_id="fixture.sdk", arguments={"value": "first"}),
                ActionInvocation(action_id="fixture.sdk", arguments={"value": "second"}),
            ],
        )
        await disable(storage, source, succeeded=True)
        await plans.confirm_plan(user_id=owner, plan_id=plan.id)
        result = await plans.execute_plan(user_id=owner, plan_id=plan.id)
        assert result.status == "partially_completed" and calls == 1
        assert result.steps[0].status == "completed"
        assert (
            result.steps[1].status == "failed"
            and result.steps[1].reason_code == "run_trace_changed"
        )
        async with storage.database.sessions() as sql:
            children = list(
                await sql.scalars(
                    select(TaskRunRecord).where(TaskRunRecord.parent_run_id == source)
                )
            )
        assert len(children) == 1 and children[0].status == "succeeded"
        assert (
            children[0].llm_attempts == 0
            and resource_usage(children[0]).get("tool_attempts", 0) == 0
        )
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_trace_depth_rejects_before_owning_another_call(backend: str, tmp_path: Path) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        await disable(storage, root)
        current = root
        now = datetime.now(UTC)
        async with storage.database.sessions.begin() as sql:
            for _ in range(31):
                identifier = uuid7()
                sql.add(
                    TaskRunRecord(
                        id=identifier,
                        user_id=owner,
                        parent_run_id=current,
                        status="running",
                        privacy_level="L1",
                        budget=RunBudgetConfig(enabled=True).model_dump(mode="json"),
                        contract={"entry": "fixture.trace.child"},
                        deadline=now + timedelta(minutes=1),
                        created_at=now,
                        updated_at=now,
                    )
                )
                await sql.flush()
                current = identifier
        trace = await capture_disabled_trace(storage.database, current, owner)
        assert trace is not None and len(trace.sources) == 32

        async def invoke(begin: Callable[[], Awaitable[None]]) -> bool:
            pytest.fail("depth denial must precede SDK dispatch")
            return False

        with run_trace_scope(trace), pytest.raises(BudgetDenied, match="run_trace_changed"):
            await execute(storage, owner, invoke)
        async with storage.database.sessions() as sql:
            assert len(list(await sql.scalars(select(TaskRunRecord)))) == 32
            assert not list(await sql.scalars(select(ModelCostRecord)))
    finally:
        await storage.close()
