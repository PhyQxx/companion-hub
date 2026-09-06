from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import create_cognition_router
from app.auth import AuthService
from app.cognition import (
    ActionDefinition,
    ActionRegistry,
    ActionRisk,
    ConfirmationPolicy,
    VerificationPolicy,
    build_builtin_action_registry,
)
from app.cognition.action_registry import HomeTargetArgs
from app.cognition.store import CognitiveStore
from app.db import Base, create_database


def test_builtin_action_catalog_has_safe_home_actions() -> None:
    registry = build_builtin_action_registry()
    definitions = {item.action_id: item for item in registry.definitions()}

    assert set(definitions) == {
        "home.light.turn_on",
        "home.light.turn_off",
        "home.switch.turn_on",
        "home.switch.turn_off",
        "home.climate.set_temperature",
        "home.light.set_brightness",
        "home.media.play",
        "home.media.pause",
        "home.media.set_volume",
        "desktop.notification.show",
        "desktop.app.open",
        "desktop.url.open",
        "system.volume.set",
        "desktop.clipboard.write",
    }
    assert definitions["home.light.turn_off"].risk == ActionRisk.A1_LOW
    assert (
        definitions["home.climate.set_temperature"].confirmation_policy
        == ConfirmationPolicy.ALWAYS
    )
    assert definitions["home.media.set_volume"].risk == ActionRisk.A2_CONFIRM
    assert definitions["desktop.notification.show"].verification_policy == "receipt"
    # HA 写动作必须写后回读；设备命令类动作以设备终态回执为准。
    assert all(
        item.verification_policy == "read_after_write"
        for action_id, item in definitions.items()
        if action_id.startswith("home.")
    )
    assert all(
        item.verification_policy == "receipt"
        for action_id, item in definitions.items()
        if action_id.startswith("desktop.") or action_id.startswith("system.")
    )


def test_registry_compiles_only_declared_arguments_and_binds_tool_action() -> None:
    registry = build_builtin_action_registry()

    compiled = registry.compile("home.light.turn_off", {"target": "客厅主灯"})

    assert compiled.arguments == {"target": "客厅主灯"}
    assert compiled.tool_arguments == {"action": "turn_off", "target": "客厅主灯"}
    assert compiled.definition.tool_name == "home_control"
    with pytest.raises(ValidationError):
        registry.compile(
            "home.light.turn_off",
            {"target": "客厅主灯", "action": "turn_on"},
        )
    with pytest.raises(LookupError, match="unknown action"):
        registry.compile("system.shell", {"command": "whoami"})

    brightness = registry.compile(
        "home.light.set_brightness",
        {"target": "客厅主灯", "brightness_pct": 40},
    )
    volume = registry.compile(
        "home.media.set_volume",
        {"target": "客厅音箱", "volume_level": 0.35},
    )
    assert brightness.tool_arguments == {
        "action": "set_brightness",
        "target": "客厅主灯",
        "brightness_pct": 40,
    }
    assert volume.tool_arguments == {
        "action": "volume_set",
        "target": "客厅音箱",
        "volume_level": 0.35,
    }
    with pytest.raises(ValidationError):
        registry.compile(
            "home.light.set_brightness",
            {"target": "客厅主灯", "brightness_pct": 101},
        )
    notification = registry.compile(
        "desktop.notification.show",
        {
            "title": "Aria",
            "body": "会议将在十分钟后开始",
            "privacy_level": "L1",
        },
    )
    assert notification.definition.tool_name == "desktop_notify"
    with pytest.raises(ValidationError):
        registry.compile(
            "desktop.notification.show",
            {
                "title": "私密",
                "body": "不应出现在桌面通知中的内容",
                "privacy_level": "L2",
            },
        )


def test_registry_rejects_duplicates_schema_drift_and_missing_compensation() -> None:
    registry = ActionRegistry()
    definition = ActionDefinition(
        action_id="test.turn_on",
        label="测试打开",
        description="用于测试的低风险动作。",
        risk=ActionRisk.A1_LOW,
        confirmation_policy=ConfirmationPolicy.PREAUTHORIZED,
        reversible=True,
        tool_name="test_tool",
        arguments_schema=HomeTargetArgs.model_json_schema(),
        verification_policy=VerificationPolicy.RECEIPT,
        verifier_id="test.receipt",
        compensation_action_id="test.turn_off",
    )
    registry.register(definition, HomeTargetArgs)
    with pytest.raises(ValueError, match="duplicate action"):
        registry.register(definition, HomeTargetArgs)
    with pytest.raises(ValueError, match="unknown compensation action"):
        registry.validate()

    drifted = definition.model_copy(
        update={
            "action_id": "test.drifted",
            "arguments_schema": {"type": "object", "properties": {}},
        }
    )
    with pytest.raises(ValueError, match="schema does not match"):
        ActionRegistry().register(drifted, HomeTargetArgs)


def test_action_definition_enforces_risk_confirmation_and_verification_contracts() -> None:
    with pytest.raises(ValidationError, match="confirmation policy"):
        ActionDefinition(
            action_id="test.unsafe",
            label="不安全声明",
            description="风险与确认策略不匹配。",
            risk=ActionRisk.A2_CONFIRM,
            confirmation_policy=ConfirmationPolicy.NEVER,
            tool_name="test_tool",
            arguments_schema=HomeTargetArgs.model_json_schema(),
        )
    with pytest.raises(ValidationError, match="requires a verifier"):
        ActionDefinition(
            action_id="test.unverified",
            label="缺少验证器",
            description="声明需要回读但没有验证器。",
            risk=ActionRisk.A1_LOW,
            confirmation_policy=ConfirmationPolicy.NEVER,
            tool_name="test_tool",
            arguments_schema=HomeTargetArgs.model_json_schema(),
            verification_policy=VerificationPolicy.READ_AFTER_WRITE,
        )


async def test_action_catalog_endpoint_requires_chat_authentication() -> None:
    database = create_database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    auth = AuthService(database)
    session = await auth.setup(
        display_name="Action owner",
        password="correct horse battery staple",
    )
    app = FastAPI()
    app.include_router(
        create_cognition_router(
            CognitiveStore(database),
            auth,
            action_registry=build_builtin_action_registry(),
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/api/v1/cognition/actions/catalog")
        response = await client.get(
            "/api/v1/cognition/actions/catalog",
            headers={"Authorization": f"Bearer {session.access_token}"},
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    catalog = {item["action_id"]: item for item in response.json()}
    assert catalog["home.light.turn_on"]["risk"] == "A1"
    assert catalog["home.climate.set_temperature"]["confirmation_policy"] == "always"
    await database.close()
