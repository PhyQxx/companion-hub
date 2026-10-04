"""A plan attached to voice chat consumes its frozen original quota."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_resource_budget import Args
from test_voice_parent_budget import discard, parent_budget
from test_voice_turn_delivery import fixture
from test_voice_websocket import FakeRecognizer

from app.cognition import ActionInvocation, ActionPlanService, ActionStepView
from app.cognition.action_registry import ActionDefinition, ActionRegistry
from app.config.models import RunBudgetConfig
from app.db import AuthSessionRecord, ModelCostRecord, TaskRunRecord
from app.harness.budget import (
    BudgetDenied,
    budget_scope,
    current_budget,
    current_tool_budget,
    tool_budget_scope,
)
from app.harness.operations import OperationPolicy
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.harness.voice_sources import VoiceSourceClaim
from app.ids import uuid7
from app.llm.contracts import ModelPricing
from app.runs import operation as operation_module
from app.runs.budget import RunModelBudget
from app.runs.costs import check_cost_allowance
from app.runs.operation import operate_with_run
from app.runs.resources import RunToolBudget, plan_tool_budget, resource_usage
from app.schemas import PrivacyLevel
from app.tools import ToolResult


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        "tool_limit",
        "money_cap",
        "actor_revoked",
        "scope_missing",
        "scope_expired",
        "source_cancelled",
        "quota_disabled",
        "actor_after_builder",
        "scope_after_builder",
        "privacy_after_builder",
        "actor_before_model",
        "actor_before_tool",
        "new_actor_delivery",
        "nested_actor",
        "actor_terminal",
        "actor_during_provider",
    ],
)
async def test_voice_plan_cannot_fund_a_fresh_child_quota(
    backend: str,
    enabled: bool,
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, manager, session, service, _, _, store = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), enabled=enabled, tts=False
    )
    context = None
    calls = 0
    cleanup_complete = False
    try:
        assert session.conversation_id is not None and manager._voice_turn_delivery is not None
        original = await parent_budget(storage, session, maximum=2)
        config = original.budget_config.model_copy(
            update={
                "max_tool_attempts": 1,
                **({"max_daily_cost": 0.001} if case == "money_cap" else {}),
            }
        )
        parent = RunModelBudget(
            storage.database,
            run_id=original.run_id,
            user_id=original.owner_id,
            config=config,
            delivery_deadline=original.delivery_deadline,
        )
        with budget_scope(parent):
            context = await manager._voice_turn_delivery.start(manager._source_claim(session))
        pending = await service.start_turn(
            session.conversation_id,
            user_id=parent.owner_id,
            text="synthetic utterance",
            privacy_level=PrivacyLevel.L1,
            parent_run_id=context.run_id,
            parent_budget_scope=context.quota_scope,
        )
        await service.run_stream(pending, discard)
        await context.finish("succeeded", "voice_reply_sent")
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
            nonlocal calls, cleanup_complete
            tool = current_tool_budget()
            assert tool is not None
            permit = await tool.reserve_tool(tool_name="synthetic.sdk", user_id=parent.owner_id)
            await start()
            calls += 1
            if case == "actor_during_provider":
                async with storage.database.sessions.begin() as sql:
                    await sql.execute(
                        update(AuthSessionRecord).values(revoked_at=datetime.now(UTC))
                    )
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0.3)
                    await tool.settle_tool(permit.call_id, reported_ok=None)
                    cleanup_complete = True
            else:
                await tool.settle_tool(permit.call_id, reported_ok=True)
            return True

        async def guard() -> None:
            return None

        async def terminal_fee_check(
            sql: AsyncSession,
            *,
            user_id: UUID,
            amount: int | None,
            currency: str | None,
            config: RunBudgetConfig,
            snapshot: RunBudgetConfig,
            now: datetime,
        ) -> None:
            await check_cost_allowance(
                sql,
                user_id=user_id,
                amount=amount,
                currency=currency,
                config=config,
                snapshot=snapshot,
                now=now,
            )
            returned = await sql.scalar(
                select(TaskRunRecord.id).where(
                    TaskRunRecord.contract["entry"].as_string() == "synthetic.plan.sdk",
                    TaskRunRecord.status == "succeeded",
                )
            )
            if returned is not None:
                await sql.execute(update(AuthSessionRecord).values(revoked_at=datetime.now(UTC)))

        if case == "actor_terminal":
            monkeypatch.setattr(operation_module, "check_cost_allowance", terminal_fee_check)

        async def runner(step: ActionStepView, owner: UUID) -> ToolResult:
            cfg = store.current
            try:
                if case in {"actor_before_tool", "new_actor_delivery"}:
                    async with storage.database.sessions.begin() as sql:
                        await sql.execute(
                            update(AuthSessionRecord).values(revoked_at=datetime.now(UTC))
                        )
                        if case == "new_actor_delivery":
                            actor = uuid7()
                            now = datetime.now(UTC)
                            sql.add(
                                AuthSessionRecord(
                                    id=actor,
                                    user_id=owner,
                                    access_hash="b" * 64,
                                    issued_at=now,
                                    last_seen_at=now,
                                    expires_at=now + timedelta(hours=1),
                                )
                            )
                    if case == "new_actor_delivery":
                        assert session.conversation_id is not None
                        assert manager._voice_turn_delivery is not None
                        await manager._voice_turn_delivery.start(
                            VoiceSourceClaim(
                                owner,
                                session.conversation_id,
                                "browser",
                                actor,
                                PrivacyLevel.L1,
                            )
                        )
                    else:
                        tool = current_tool_budget()
                        assert tool is not None
                        await tool.reserve_tool(tool_name="synthetic.direct", user_id=owner)
                    pytest.fail("revoked source admitted")
                if case == "actor_before_model":
                    budget = current_budget()
                    # Plan clears the confirmation model; adopt its source tool explicitly.
                    assert budget is None
                    tool = current_tool_budget()
                    assert tool is not None
                    from app.runs.parent_budget import current_parent_budget

                    model = current_parent_budget(storage.database, owner)
                    assert model is not None
                    async with storage.database.sessions.begin() as sql:
                        await sql.execute(
                            update(AuthSessionRecord).values(revoked_at=datetime.now(UTC))
                        )
                    await model.reserve(
                        endpoint="synthetic.model",
                        tokens=1,
                        final=True,
                        pricing=ModelPricing(input_rate=0, output_rate=0, currency="CNY"),
                    )
                await operate_with_run(
                    storage.database,
                    OperationPolicy(
                        cfg.version,
                        tuple(cfg.config.run_budget.model_dump().items()),
                        UnitCostQuote(
                            pricing=UnitPricing(
                                unit="request", currency="CNY", rate_per_unit=".002"
                            ),
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
                    cooperative=True,
                )
                return ToolResult(ok=True, tool_name=step.tool_name, latency_ms=0)
            except BudgetDenied as error:
                return ToolResult(
                    admission_status="not_admitted",
                    ok=False,
                    tool_name=step.tool_name,
                    latency_ms=0,
                    reason_code=error.reason_code,
                )

        plans = ActionPlanService(storage.database, registry, runner=runner)

        async def build(run: UUID, owner: UUID, deadline: datetime) -> RunToolBudget | None:
            budget = await plan_tool_budget(
                storage.database, run, owner, deadline, store.current.config.run_budget
            )
            if case == "nested_actor":
                assert budget is not None and manager._voice_turn_delivery is not None
                assert session.conversation_id is not None
                actor = uuid7()
                now = datetime.now(UTC)
                async with storage.database.sessions.begin() as sql:
                    sql.add(
                        AuthSessionRecord(
                            id=actor,
                            user_id=owner,
                            access_hash="c" * 64,
                            issued_at=now,
                            last_seen_at=now,
                            expires_at=now + timedelta(hours=1),
                        )
                    )
                with budget_scope(None), tool_budget_scope(budget):
                    nested = await manager._voice_turn_delivery.start(
                        VoiceSourceClaim(
                            owner,
                            session.conversation_id,
                            "browser",
                            actor,
                            PrivacyLevel.L1,
                        )
                    )
                try:
                    followup = await service.start_turn(
                        session.conversation_id,
                        user_id=owner,
                        text="synthetic nested utterance",
                        privacy_level=PrivacyLevel.L1,
                        parent_run_id=nested.run_id,
                        parent_budget_scope=nested.quota_scope,
                    )
                    await service.run_stream(followup, discard)
                    await nested.finish("succeeded", "voice_reply_sent")
                    budget = await plan_tool_budget(
                        storage.database,
                        followup.turn_id,
                        owner,
                        deadline,
                        store.current.config.run_budget,
                    )
                    assert budget is not None and len(budget.origins) == 2
                    async with storage.database.sessions.begin() as sql:
                        await sql.execute(
                            update(AuthSessionRecord)
                            .where(AuthSessionRecord.id == session.principal.session_id)
                            .values(revoked_at=datetime.now(UTC))
                        )
                finally:
                    await nested.finish("cancelled", "synthetic_cleanup")
            if case.endswith("after_builder"):
                async with storage.database.sessions.begin() as sql:
                    child = await sql.get_one(TaskRunRecord, run)
                    if case == "actor_after_builder":
                        await sql.execute(
                            update(AuthSessionRecord).values(revoked_at=datetime.now(UTC))
                        )
                    elif case == "scope_after_builder":
                        child.contract = {**child.contract, "quota_scope": None}
                    else:
                        child.privacy_level = "L2"
            return budget

        plans.set_tool_budget_builder(build)
        plan = await plans.create_plan(
            user_id=parent.owner_id,
            source_turn_id=pending.turn_id,
            invocations=[
                ActionInvocation(action_id="fixture.sdk", arguments={"value": f"synthetic-{index}"})
                for index in range(2)
            ],
        )
        async with storage.database.sessions.begin() as sql:
            root = await sql.get_one(TaskRunRecord, parent.run_id)
            root.status = "succeeded"
        confirmation = await parent_budget(storage, session)
        with budget_scope(confirmation):
            await plans.confirm_plan(user_id=parent.owner_id, plan_id=plan.id)
            async with storage.database.sessions.begin() as sql:
                child = await sql.get_one(TaskRunRecord, pending.turn_id)
                if case == "actor_revoked":
                    await sql.execute(
                        update(AuthSessionRecord).values(revoked_at=datetime.now(UTC))
                    )
                elif case == "scope_missing":
                    child.contract = {
                        key: value for key, value in child.contract.items() if key != "quota_scope"
                    }
                elif case == "scope_expired":
                    scope = dict(child.contract["quota_scope"])
                    scope["delivery_deadline"] = (
                        datetime.now(UTC) - timedelta(seconds=1)
                    ).isoformat()
                    child.contract = {**child.contract, "quota_scope": scope}
                elif case == "source_cancelled":
                    child.contract = {**child.contract, "work_cancel_requested": True}
                elif case == "quota_disabled":
                    root = await sql.get_one(TaskRunRecord, parent.run_id)
                    root.budget = {**root.budget, "enabled": False} if root.budget else None
            result = await plans.execute_plan(user_id=parent.owner_id, plan_id=plan.id)
        assert result.status != "completed"
        expected_calls = (
            1 if case in {"tool_limit", "actor_during_provider", "actor_terminal"} else 0
        )
        assert calls == expected_calls
        if case == "actor_during_provider":
            assert cleanup_complete
        async with storage.database.sessions() as sql:
            root = await sql.get_one(TaskRunRecord, parent.run_id)
            child = await sql.get_one(TaskRunRecord, pending.turn_id)
            later = await sql.get_one(TaskRunRecord, confirmation.run_id)
            provider_runs = list(
                await sql.scalars(
                    select(TaskRunRecord).where(
                        TaskRunRecord.contract["entry"].as_string() == "synthetic.plan.sdk"
                    )
                )
            )
            fees = list(
                await sql.scalars(select(ModelCostRecord).where(ModelCostRecord.unit == "request"))
            )
        assert resource_usage(root).get("tool_attempts", 0) == expected_calls
        assert (
            resource_usage(child).get("tool_attempts", 0)
            == resource_usage(later).get("tool_attempts", 0)
            == 0
        )
        for provider_run in provider_runs:
            origin = provider_run.contract["quota_origins"][0]
            assert origin["source_run_id"] == str(pending.turn_id)
            assert origin["parent_run_id"] == str(context.run_id)
        assert sum(fee.charged_micros or 0 for fee in fees) == 2000 * expected_calls
    finally:
        if context is not None:
            await context.finish("cancelled", "synthetic_cleanup")
        await service.drain_background_work()
        await storage.close()
