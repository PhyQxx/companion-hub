from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import JsonValue
from sqlalchemy import update

from app.api import create_cognition_router
from app.auth import AuthService
from app.cognition import (
    ActionInvocation,
    ActionPlanService,
    ActionPlanStatus,
    ActionRunResult,
    ActionStepStatus,
    ActionStepView,
    ActionVerificationStatus,
    ToolActionRunner,
    build_builtin_action_registry,
)
from app.cognition.store import CognitiveStore
from app.config import HomeAssistantEntityConfig
from app.db import (
    ActionPlanRecord,
    ActionStepRecord,
    AppUserRecord,
    Base,
    Database,
    create_database,
)
from app.home_assistant import HomeAssistantError, HomeAssistantState, HomeControlTool
from app.ids import uuid7
from app.tools import DesktopNotifyTool, ToolExecutor, ToolRegistry, ToolResult


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
async def user_id(database: Database) -> UUID:
    value = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=value, display_name="Action owner", status="active"))
    return value


async def test_plan_requires_confirmation_then_becomes_ready(
    database: Database,
    user_id: UUID,
) -> None:
    service = ActionPlanService(database, build_builtin_action_registry())
    plan = await service.create_plan(
        user_id=user_id,
        title="睡眠准备",
        invocations=[
            ActionInvocation(
                action_id="home.light.turn_off",
                arguments={"target": "客厅主灯"},
            ),
            ActionInvocation(
                action_id="home.climate.set_temperature",
                arguments={"target": "卧室空调", "temperature_c": 25},
            ),
        ],
        idempotency_key="sleep-plan-0001",
    )

    assert plan.status == ActionPlanStatus.AWAITING_CONFIRMATION
    assert [step.status for step in plan.steps] == [
        ActionStepStatus.AWAITING_CONFIRMATION,
        ActionStepStatus.AWAITING_CONFIRMATION,
    ]
    assert plan.steps[0].tool_arguments == {"target": "客厅主灯", "action": "turn_off"}

    confirmed = await service.confirm_plan(user_id=user_id, plan_id=plan.id)
    assert confirmed.status == ActionPlanStatus.READY
    assert confirmed.confirmed_at is not None
    assert all(step.status == ActionStepStatus.READY for step in confirmed.steps)


async def test_preauthorized_low_risk_plan_is_ready_but_a2_still_waits(
    database: Database,
    user_id: UUID,
) -> None:
    service = ActionPlanService(
        database,
        build_builtin_action_registry(),
        preauthorized_action_ids=frozenset({"home.light.turn_off"}),
    )
    light = await service.create_plan(
        user_id=user_id,
        invocations=[
            ActionInvocation(
                action_id="home.light.turn_off",
                arguments={"target": "书房灯"},
            )
        ],
    )
    climate = await service.create_plan(
        user_id=user_id,
        invocations=[
            ActionInvocation(
                action_id="home.climate.set_temperature",
                arguments={"target": "卧室空调", "temperature_c": 24},
            )
        ],
    )

    assert light.status == ActionPlanStatus.READY
    assert climate.status == ActionPlanStatus.AWAITING_CONFIRMATION


async def test_plan_idempotency_and_cancellation_are_deterministic(
    database: Database,
    user_id: UUID,
) -> None:
    service = ActionPlanService(database, build_builtin_action_registry())
    invocation = ActionInvocation(
        action_id="home.switch.turn_off",
        arguments={"target": "鱼缸加热开关"},
    )
    first = await service.create_plan(
        user_id=user_id,
        invocations=[invocation],
        idempotency_key="switch-plan-0001",
    )
    replay = await service.create_plan(
        user_id=user_id,
        invocations=[invocation],
        idempotency_key="switch-plan-0001",
    )
    assert replay.id == first.id

    with pytest.raises(ValueError, match="another action plan"):
        await service.create_plan(
            user_id=user_id,
            invocations=[
                ActionInvocation(
                    action_id="home.switch.turn_on",
                    arguments={"target": "鱼缸加热开关"},
                )
            ],
            idempotency_key="switch-plan-0001",
        )

    cancelled = await service.cancel_plan(user_id=user_id, plan_id=first.id)
    assert cancelled.status == ActionPlanStatus.CANCELLED
    assert cancelled.cancel_reason == "user_cancelled"
    assert cancelled.steps[0].status == ActionStepStatus.CANCELLED
    with pytest.raises(ValueError, match="can no longer be cancelled"):
        await service.cancel_plan(user_id=user_id, plan_id=first.id)


async def test_expired_plan_cannot_be_confirmed_and_stays_expired(
    database: Database,
    user_id: UUID,
) -> None:
    service = ActionPlanService(database, build_builtin_action_registry())
    plan = await service.create_plan(
        user_id=user_id,
        invocations=[
            ActionInvocation(
                action_id="home.light.turn_off",
                arguments={"target": "玄关灯"},
            )
        ],
    )
    async with database.sessions.begin() as session:
        await session.execute(
            update(ActionPlanRecord)
            .where(ActionPlanRecord.id == plan.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    with pytest.raises(ValueError, match="expired"):
        await service.confirm_plan(user_id=user_id, plan_id=plan.id)
    expired = await service.get_plan(user_id=user_id, plan_id=plan.id)
    assert expired.status == ActionPlanStatus.EXPIRED
    assert expired.steps[0].status == ActionStepStatus.EXPIRED


async def test_action_plan_api_create_confirm_and_require_authentication(
    database: Database,
) -> None:
    auth = AuthService(database)
    owner = await auth.setup(
        display_name="Plan owner",
        password="correct horse battery staple",
    )
    registry = build_builtin_action_registry()

    async def runner(step: ActionStepView, runner_user_id: UUID) -> ToolResult:
        assert runner_user_id == owner.principal.user_id
        return ToolResult(ok=True, tool_name=step.tool_name, latency_ms=1)

    service = ActionPlanService(database, registry, runner=runner)
    app = FastAPI()
    app.include_router(
        create_cognition_router(
            CognitiveStore(database),
            auth,
            action_registry=registry,
            action_plan_service=service,
        )
    )
    headers = {"Authorization": f"Bearer {owner.access_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/v1/cognition/action-plans",
            headers=headers,
            json={
                "title": "离家",
                "idempotency_key": "leave-home-0001",
                "steps": [
                    {
                        "action_id": "home.light.turn_off",
                        "arguments": {"target": "全部灯光"},
                    }
                ],
            },
        )
        confirmed = await client.post(
            f"/api/v1/cognition/action-plans/{created.json()['id']}/confirm",
            headers=headers,
        )
        executed = await client.post(
            f"/api/v1/cognition/action-plans/{created.json()['id']}/execute",
            headers=headers,
        )
        undo = await client.post(
            f"/api/v1/cognition/action-plans/{created.json()['id']}/undo",
            headers=headers,
        )
        unauthorized = await client.get(
            f"/api/v1/cognition/action-plans/{created.json()['id']}"
        )

    assert created.status_code == 201
    assert created.json()["status"] == "awaiting_confirmation"
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "ready"
    assert executed.status_code == 200
    assert executed.json()["status"] == "completed"
    assert undo.status_code == 201
    assert undo.json()["plan_kind"] == "compensation"
    assert undo.json()["status"] == "awaiting_confirmation"
    assert unauthorized.status_code == 401


async def test_confirmed_plan_executes_steps_in_order(database: Database, user_id: UUID) -> None:
    calls: list[str] = []

    async def runner(step: ActionStepView, runner_user_id: UUID) -> ToolResult:
        assert runner_user_id == user_id
        calls.append(step.action_id)
        return ToolResult(
            ok=True,
            tool_name=step.tool_name,
            provider="test",
            data={"target": step.tool_arguments["target"]},
            latency_ms=1,
        )

    service = ActionPlanService(database, build_builtin_action_registry(), runner=runner)
    plan = await service.create_plan(
        user_id=user_id,
        invocations=[
            ActionInvocation(
                action_id="home.light.turn_off",
                arguments={"target": "客厅灯"},
            ),
            ActionInvocation(
                action_id="home.switch.turn_off",
                arguments={"target": "电视电源"},
            ),
        ],
    )
    await service.confirm_plan(user_id=user_id, plan_id=plan.id)

    completed = await service.execute_plan(user_id=user_id, plan_id=plan.id)

    assert completed.status == ActionPlanStatus.COMPLETED
    assert all(step.status == ActionStepStatus.COMPLETED for step in completed.steps)
    assert calls == ["home.light.turn_off", "home.switch.turn_off"]


async def test_failed_step_stops_plan_and_skips_remaining(
    database: Database,
    user_id: UUID,
) -> None:
    async def runner(step: ActionStepView, runner_user_id: UUID) -> ToolResult:
        del runner_user_id
        ok = step.position == 1
        return ToolResult(
            ok=ok,
            tool_name=step.tool_name,
            reason_code=None if ok else "simulated_failure",
            latency_ms=1,
        )

    service = ActionPlanService(database, build_builtin_action_registry(), runner=runner)
    plan = await service.create_plan(
        user_id=user_id,
        invocations=[
            ActionInvocation(action_id="home.light.turn_off", arguments={"target": "灯1"}),
            ActionInvocation(action_id="home.light.turn_off", arguments={"target": "灯2"}),
            ActionInvocation(action_id="home.light.turn_off", arguments={"target": "灯3"}),
        ],
    )
    await service.confirm_plan(user_id=user_id, plan_id=plan.id)

    result = await service.execute_plan(user_id=user_id, plan_id=plan.id)

    assert result.status == ActionPlanStatus.PARTIALLY_COMPLETED
    assert [step.status for step in result.steps] == [
        ActionStepStatus.COMPLETED,
        ActionStepStatus.FAILED,
        ActionStepStatus.SKIPPED,
    ]
    assert result.steps[1].reason_code == "simulated_failure"
    assert result.steps[2].reason_code == "previous_step_failed"


async def test_timeout_becomes_unknown_outcome_without_retry(
    database: Database,
    user_id: UUID,
) -> None:
    calls = 0

    async def runner(step: ActionStepView, runner_user_id: UUID) -> ToolResult:
        nonlocal calls
        del step, runner_user_id
        calls += 1
        await asyncio.sleep(1)
        raise AssertionError("timeout should cancel the runner")

    service = ActionPlanService(database, build_builtin_action_registry(), runner=runner)
    plan = await service.create_plan(
        user_id=user_id,
        invocations=[
            ActionInvocation(
                action_id="home.light.turn_off",
                arguments={"target": "超时测试灯"},
            )
        ],
    )
    await service.confirm_plan(user_id=user_id, plan_id=plan.id)
    async with database.sessions.begin() as session:
        await session.execute(
            update(ActionStepRecord)
            .where(ActionStepRecord.plan_id == plan.id)
            .values(timeout_seconds=0)
        )

    result = await service.execute_plan(user_id=user_id, plan_id=plan.id)

    assert calls == 1
    assert result.status == ActionPlanStatus.FAILED
    assert result.steps[0].status == ActionStepStatus.UNKNOWN_OUTCOME
    assert result.steps[0].reason_code == "action_timeout_unknown_outcome"


async def test_verified_execution_persists_minimal_readback_evidence(
    database: Database,
    user_id: UUID,
) -> None:
    async def runner(step: ActionStepView, runner_user_id: UUID) -> ActionRunResult:
        assert runner_user_id == user_id
        return ActionRunResult(
            execution=ToolResult(ok=True, tool_name=step.tool_name, latency_ms=1),
            verification_status=ActionVerificationStatus.VERIFIED,
            verification_result={
                "entity_id": "light.desk",
                "expected_state": "off",
                "observed_state": "off",
            },
        )

    service = ActionPlanService(database, build_builtin_action_registry(), runner=runner)
    plan = await service.create_plan(
        user_id=user_id,
        invocations=[
            ActionInvocation(
                action_id="home.light.turn_off",
                arguments={"target": "书桌灯"},
            )
        ],
    )
    await service.confirm_plan(user_id=user_id, plan_id=plan.id)

    result = await service.execute_plan(user_id=user_id, plan_id=plan.id)

    assert result.status == ActionPlanStatus.COMPLETED
    assert result.steps[0].verification_status == ActionVerificationStatus.VERIFIED
    assert result.steps[0].verified_at is not None
    assert result.steps[0].verification_result == {
        "entity_id": "light.desk",
        "expected_state": "off",
        "observed_state": "off",
    }


async def test_inconclusive_readback_becomes_unknown_outcome(
    database: Database,
    user_id: UUID,
) -> None:
    async def runner(step: ActionStepView, runner_user_id: UUID) -> ActionRunResult:
        del runner_user_id
        return ActionRunResult(
            execution=ToolResult(ok=True, tool_name=step.tool_name, latency_ms=1),
            verification_status=ActionVerificationStatus.INCONCLUSIVE,
            verification_result={
                "expected_state": "off",
                "observed_state": "on",
            },
            verification_reason_code="action_state_mismatch_unknown_outcome",
        )

    service = ActionPlanService(database, build_builtin_action_registry(), runner=runner)
    plan = await service.create_plan(
        user_id=user_id,
        invocations=[
            ActionInvocation(
                action_id="home.light.turn_off",
                arguments={"target": "走廊灯"},
            ),
            ActionInvocation(
                action_id="home.switch.turn_off",
                arguments={"target": "电视电源"},
            ),
        ],
    )
    await service.confirm_plan(user_id=user_id, plan_id=plan.id)

    result = await service.execute_plan(user_id=user_id, plan_id=plan.id)

    assert result.status == ActionPlanStatus.FAILED
    assert result.steps[0].status == ActionStepStatus.UNKNOWN_OUTCOME
    assert result.steps[0].verification_status == ActionVerificationStatus.INCONCLUSIVE
    assert result.steps[1].status == ActionStepStatus.SKIPPED


async def test_undo_plan_reverses_completed_reversible_steps_and_is_idempotent(
    database: Database,
    user_id: UUID,
) -> None:
    async def runner(step: ActionStepView, runner_user_id: UUID) -> ToolResult:
        del runner_user_id
        return ToolResult(ok=True, tool_name=step.tool_name, latency_ms=1)

    service = ActionPlanService(database, build_builtin_action_registry(), runner=runner)
    original = await service.create_plan(
        user_id=user_id,
        title="离家",
        invocations=[
            ActionInvocation(
                action_id="home.light.turn_off",
                arguments={"target": "客厅灯"},
            ),
            ActionInvocation(
                action_id="home.switch.turn_on",
                arguments={"target": "安防开关"},
            ),
        ],
    )
    await service.confirm_plan(user_id=user_id, plan_id=original.id)
    completed = await service.execute_plan(user_id=user_id, plan_id=original.id)

    undo = await service.create_undo_plan(user_id=user_id, plan_id=completed.id)
    replay = await service.create_undo_plan(user_id=user_id, plan_id=completed.id)
    refreshed = await service.get_plan(user_id=user_id, plan_id=completed.id)

    assert replay.id == undo.id
    assert refreshed.undo_plan_id == undo.id
    assert undo.plan_kind == "compensation"
    assert undo.source_plan_id == completed.id
    assert undo.status == ActionPlanStatus.AWAITING_CONFIRMATION
    assert [step.action_id for step in undo.steps] == [
        "home.switch.turn_off",
        "home.light.turn_on",
    ]
    assert [step.compensates_step_id for step in undo.steps] == [
        completed.steps[1].id,
        completed.steps[0].id,
    ]


async def test_tool_action_runner_reads_back_home_state(
    database: Database,
    user_id: UUID,
) -> None:
    policy = HomeAssistantEntityConfig.model_validate(
        {
            "entity_id": "light.desk",
            "display_name": "书桌灯",
            "aliases": [],
            "read_allowed": True,
            "allowed_actions": ["turn_on", "turn_off"],
            "privacy_level": "L2",
        }
    )

    class Provider:
        state = HomeAssistantState("light.desk", "on", {}, None, None)

        def resolve(self, target: str) -> HomeAssistantEntityConfig:
            if target not in {policy.entity_id, policy.display_name}:
                raise HomeAssistantError("ha_entity_not_found")
            return policy

        def get_state(self, entity_id: str) -> HomeAssistantState:
            if entity_id != policy.entity_id:
                raise HomeAssistantError("ha_read_denied")
            return self.state

        async def control(
            self,
            target: str,
            action: str,
            *,
            service_data: dict[str, object] | None = None,
        ) -> HomeAssistantEntityConfig:
            del service_data
            self.resolve(target)
            self.state = HomeAssistantState(
                policy.entity_id,
                "on" if action == "turn_on" else "off",
                {},
                None,
                None,
            )
            return policy

    provider = Provider()
    runner = ToolActionRunner(
        ToolExecutor(ToolRegistry([HomeControlTool(provider)])),
        home_state_provider=provider,
    )
    service = ActionPlanService(
        database,
        build_builtin_action_registry(),
        runner=runner,
    )
    plan = await service.create_plan(
        user_id=user_id,
        invocations=[
            ActionInvocation(
                action_id="home.light.turn_off",
                arguments={"target": "书桌灯"},
            )
        ],
    )
    await service.confirm_plan(user_id=user_id, plan_id=plan.id)

    result = await service.execute_plan(user_id=user_id, plan_id=plan.id)

    assert result.status == ActionPlanStatus.COMPLETED
    assert result.steps[0].verification_status == ActionVerificationStatus.VERIFIED
    assert result.steps[0].verification_result == {
        "entity_id": "light.desk",
        "expected_state": "off",
        "observed_state": "off",
    }


@pytest.mark.parametrize(
    ("entity_id", "action_id", "arguments", "expected_evidence"),
    [
        (
            "light.desk",
            "home.light.set_brightness",
            {"target": "测试设备", "brightness_pct": 40},
            {"expected_brightness_pct": 40, "observed_brightness_pct": 40},
        ),
        (
            "media_player.living_room",
            "home.media.play",
            {"target": "测试设备"},
            {"expected_state": "playing", "observed_state": "playing"},
        ),
        (
            "media_player.living_room",
            "home.media.set_volume",
            {"target": "测试设备", "volume_level": 0.35},
            {"expected_volume_level": 0.35, "observed_volume_level": 0.35},
        ),
    ],
)
async def test_tool_action_runner_verifies_extended_home_actions(
    database: Database,
    user_id: UUID,
    entity_id: str,
    action_id: str,
    arguments: dict[str, object],
    expected_evidence: dict[str, object],
) -> None:
    semantic_action = {
        "home.light.set_brightness": "set_brightness",
        "home.media.play": "play",
        "home.media.set_volume": "volume_set",
    }[action_id]
    policy = HomeAssistantEntityConfig.model_validate(
        {
            "entity_id": entity_id,
            "display_name": "测试设备",
            "read_allowed": True,
            "allowed_actions": [semantic_action],
            "privacy_level": "L2",
        }
    )

    class Provider:
        state = HomeAssistantState(entity_id, "idle", {}, None, None)

        def resolve(self, target: str) -> HomeAssistantEntityConfig:
            if target not in {policy.entity_id, policy.display_name}:
                raise HomeAssistantError("ha_entity_not_found")
            return policy

        def get_state(self, requested_entity_id: str) -> HomeAssistantState:
            if requested_entity_id != policy.entity_id:
                raise HomeAssistantError("ha_read_denied")
            return self.state

        async def control(
            self,
            target: str,
            action: str,
            *,
            service_data: dict[str, object] | None = None,
        ) -> HomeAssistantEntityConfig:
            self.resolve(target)
            state = "playing" if action == "play" else "on"
            attributes: dict[str, object] = {}
            if action == "set_brightness":
                attributes["brightness"] = round(
                    float(cast(int | float, (service_data or {})["brightness_pct"]))
                    * 255
                    / 100
                )
            elif action == "volume_set":
                state = "playing"
                attributes["volume_level"] = (service_data or {})["volume_level"]
            self.state = HomeAssistantState(entity_id, state, attributes, None, None)
            return policy

    provider = Provider()
    service = ActionPlanService(
        database,
        build_builtin_action_registry(),
        runner=ToolActionRunner(
            ToolExecutor(ToolRegistry([HomeControlTool(provider)])),
            home_state_provider=provider,
        ),
    )
    plan = await service.create_plan(
        user_id=user_id,
        invocations=[ActionInvocation(action_id=action_id, arguments=arguments)],
    )
    await service.confirm_plan(user_id=user_id, plan_id=plan.id)

    result = await service.execute_plan(user_id=user_id, plan_id=plan.id)

    assert result.status == ActionPlanStatus.COMPLETED
    assert result.steps[0].verification_status == ActionVerificationStatus.VERIFIED
    assert result.steps[0].verification_result == {
        "entity_id": entity_id,
        **expected_evidence,
    }


async def test_desktop_notification_uses_device_receipt_and_step_idempotency(
    database: Database,
    user_id: UUID,
) -> None:
    device_id = uuid7()

    @dataclass
    class Device:
        id: UUID

    @dataclass
    class Command:
        id: UUID
        status: str
        reason_code: str | None = None

    class Resolver:
        async def resolve(
            self,
            *,
            owner_user_id: UUID,
            target: str | UUID | None,
            capability: str,
        ) -> Device:
            assert owner_user_id == user_id
            assert target == "我的电脑"
            assert capability == "notification.show"
            return Device(device_id)

    class Gateway:
        def __init__(self) -> None:
            self.issued: list[dict[str, object]] = []

        async def issue(
            self,
            *,
            device_id: UUID,
            command: str,
            args: dict[str, JsonValue],
            idempotency_key: str,
            ttl_seconds: int,
        ) -> Command:
            self.issued.append(
                {
                    "device_id": device_id,
                    "command": command,
                    "args": args,
                    "idempotency_key": idempotency_key,
                    "ttl_seconds": ttl_seconds,
                }
            )
            return Command(uuid7(), "sent")

        async def wait_for_terminal(
            self,
            command_id: UUID,
            *,
            timeout_seconds: float | None = None,
        ) -> Command:
            assert timeout_seconds == 9
            return Command(command_id, "succeeded")

    gateway = Gateway()
    service = ActionPlanService(
        database,
        build_builtin_action_registry(),
        runner=ToolActionRunner(
            ToolExecutor(ToolRegistry([DesktopNotifyTool(Resolver(), gateway)]))
        ),
    )
    plan = await service.create_plan(
        user_id=user_id,
        invocations=[
            ActionInvocation(
                action_id="desktop.notification.show",
                arguments={
                    "title": "Aria",
                    "body": "会议将在十分钟后开始",
                    "privacy_level": "L1",
                    "target": "我的电脑",
                },
            )
        ],
    )
    await service.confirm_plan(user_id=user_id, plan_id=plan.id)

    result = await service.execute_plan(user_id=user_id, plan_id=plan.id)

    assert result.status == ActionPlanStatus.COMPLETED
    assert result.steps[0].verification_status == ActionVerificationStatus.VERIFIED
    assert result.steps[0].verification_result is not None
    assert result.steps[0].verification_result["status"] == "succeeded"
    assert gateway.issued == [
        {
            "device_id": device_id,
            "command": "notification.show",
            "args": {
                "title": "Aria",
                "body": "会议将在十分钟后开始",
                "privacy_level": "L1",
            },
            "idempotency_key": result.steps[0].idempotency_key,
            "ttl_seconds": 8,
        }
    ]
