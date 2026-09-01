from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.schemas import PrivacyLevel
from app.schemas.common import StrictModel, TokenName
from app.tools.desktop import DesktopNotifyArgs


class ActionRisk(StrEnum):
    A0_READ = "A0"
    A1_LOW = "A1"
    A2_CONFIRM = "A2"
    A3_PROHIBITED = "A3"


class ConfirmationPolicy(StrEnum):
    NEVER = "never"
    PREAUTHORIZED = "preauthorized"
    ALWAYS = "always"
    PROHIBITED = "prohibited"


class VerificationPolicy(StrEnum):
    NONE = "none"
    RECEIPT = "receipt"
    READ_AFTER_WRITE = "read_after_write"


class ActionDefinition(StrictModel):
    schema_version: Literal[1] = 1
    action_id: TokenName
    label: Annotated[str, Field(min_length=1, max_length=160)]
    description: Annotated[str, Field(min_length=1, max_length=500)]
    risk: ActionRisk
    confirmation_policy: ConfirmationPolicy
    reversible: bool = False
    tool_name: TokenName
    arguments_schema: dict[str, JsonValue]
    bound_arguments: dict[str, JsonValue] = Field(default_factory=dict)
    timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 30
    verification_policy: VerificationPolicy = VerificationPolicy.NONE
    verifier_id: TokenName | None = None
    compensation_action_id: TokenName | None = None
    max_privacy_level: PrivacyLevel = PrivacyLevel.L2
    idempotency_scope: Literal["owner_action_arguments"] = "owner_action_arguments"

    @model_validator(mode="after")
    def validate_safety_contract(self) -> ActionDefinition:
        expected_confirmation = {
            ActionRisk.A0_READ: {ConfirmationPolicy.NEVER},
            ActionRisk.A1_LOW: {
                ConfirmationPolicy.NEVER,
                ConfirmationPolicy.PREAUTHORIZED,
            },
            ActionRisk.A2_CONFIRM: {ConfirmationPolicy.ALWAYS},
            ActionRisk.A3_PROHIBITED: {ConfirmationPolicy.PROHIBITED},
        }
        if self.confirmation_policy not in expected_confirmation[self.risk]:
            raise ValueError("confirmation policy does not match action risk")
        if self.reversible and self.compensation_action_id is None:
            raise ValueError("reversible actions require a compensation action")
        if self.verification_policy is VerificationPolicy.NONE and self.verifier_id is not None:
            raise ValueError("verifier requires a verification policy")
        if (
            self.verification_policy is not VerificationPolicy.NONE
            and self.verifier_id is None
        ):
            raise ValueError("verification policy requires a verifier")
        return self


@dataclass(frozen=True, slots=True)
class RegisteredAction:
    definition: ActionDefinition
    arguments_model: type[BaseModel]


class CompiledAction(StrictModel):
    definition: ActionDefinition
    arguments: dict[str, JsonValue]
    tool_arguments: dict[str, JsonValue]


class ActionRegistry:
    """Static action safety catalog layered above executable tools."""

    def __init__(self) -> None:
        self._actions: dict[str, RegisteredAction] = {}

    def register(
        self,
        definition: ActionDefinition,
        arguments_model: type[BaseModel],
    ) -> None:
        if definition.action_id in self._actions:
            raise ValueError(f"duplicate action: {definition.action_id}")
        dynamic_fields = set(arguments_model.model_fields)
        collisions = dynamic_fields.intersection(definition.bound_arguments)
        if collisions:
            raise ValueError(
                "bound arguments collide with dynamic arguments: "
                + ", ".join(sorted(collisions))
            )
        if definition.arguments_schema != arguments_model.model_json_schema():
            raise ValueError("action arguments schema does not match arguments model")
        self._actions[definition.action_id] = RegisteredAction(
            definition=definition,
            arguments_model=arguments_model,
        )

    def validate(self) -> None:
        for registered in self._actions.values():
            definition = registered.definition
            compensation_id = definition.compensation_action_id
            if compensation_id is None:
                continue
            compensation = self._actions.get(compensation_id)
            if compensation is None:
                raise ValueError(
                    f"unknown compensation action for {definition.action_id}: {compensation_id}"
                )
            if compensation.definition.risk is ActionRisk.A3_PROHIBITED:
                raise ValueError("compensation action cannot be prohibited")

    def get(self, action_id: str) -> RegisteredAction | None:
        return self._actions.get(action_id)

    def require(self, action_id: str) -> RegisteredAction:
        registered = self.get(action_id)
        if registered is None:
            raise LookupError(f"unknown action: {action_id}")
        return registered

    def definitions(self) -> list[ActionDefinition]:
        return [item.definition for item in self._actions.values()]

    def compile(self, action_id: str, arguments: dict[str, object]) -> CompiledAction:
        registered = self.require(action_id)
        if registered.definition.risk is ActionRisk.A3_PROHIBITED:
            raise PermissionError("prohibited actions cannot be compiled")
        validated = registered.arguments_model.model_validate(arguments)
        dynamic = validated.model_dump(mode="json", exclude_none=True)
        tool_arguments: dict[str, JsonValue] = {
            **registered.definition.bound_arguments,
            **dynamic,
        }
        return CompiledAction(
            definition=registered.definition,
            arguments=dynamic,
            tool_arguments=tool_arguments,
        )


class HomeTargetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Annotated[str, Field(min_length=1, max_length=255)]


class HomeTemperatureArgs(HomeTargetArgs):
    temperature_c: Annotated[float, Field(ge=16, le=30)]


class HomeBrightnessArgs(HomeTargetArgs):
    brightness_pct: Annotated[int, Field(ge=1, le=100)]


class HomeVolumeArgs(HomeTargetArgs):
    volume_level: Annotated[float, Field(ge=0, le=1)]


def _home_action(
    *,
    action_id: str,
    label: str,
    description: str,
    risk: ActionRisk,
    confirmation_policy: ConfirmationPolicy,
    arguments_model: type[BaseModel],
    action: str,
    compensation_action_id: str | None = None,
) -> tuple[ActionDefinition, type[BaseModel]]:
    reversible = compensation_action_id is not None
    return (
        ActionDefinition(
            action_id=action_id,
            label=label,
            description=description,
            risk=risk,
            confirmation_policy=confirmation_policy,
            reversible=reversible,
            tool_name="home_control",
            arguments_schema=arguments_model.model_json_schema(),
            bound_arguments={"action": action},
            timeout_seconds=30,
            verification_policy=VerificationPolicy.READ_AFTER_WRITE,
            verifier_id="home.entity_state",
            compensation_action_id=compensation_action_id,
        ),
        arguments_model,
    )


def build_builtin_action_registry() -> ActionRegistry:
    registry = ActionRegistry()
    registrations: list[tuple[ActionDefinition, type[BaseModel]]] = [
        _home_action(
            action_id="home.light.turn_on",
            label="打开灯光",
            description="打开后台已授权控制的 Home Assistant 灯光。",
            risk=ActionRisk.A1_LOW,
            confirmation_policy=ConfirmationPolicy.PREAUTHORIZED,
            arguments_model=HomeTargetArgs,
            action="turn_on",
            compensation_action_id="home.light.turn_off",
        ),
        _home_action(
            action_id="home.light.turn_off",
            label="关闭灯光",
            description="关闭后台已授权控制的 Home Assistant 灯光。",
            risk=ActionRisk.A1_LOW,
            confirmation_policy=ConfirmationPolicy.PREAUTHORIZED,
            arguments_model=HomeTargetArgs,
            action="turn_off",
            compensation_action_id="home.light.turn_on",
        ),
        _home_action(
            action_id="home.switch.turn_on",
            label="打开开关",
            description="打开后台已授权控制的 Home Assistant 开关。",
            risk=ActionRisk.A1_LOW,
            confirmation_policy=ConfirmationPolicy.PREAUTHORIZED,
            arguments_model=HomeTargetArgs,
            action="turn_on",
            compensation_action_id="home.switch.turn_off",
        ),
        _home_action(
            action_id="home.switch.turn_off",
            label="关闭开关",
            description="关闭后台已授权控制的 Home Assistant 开关。",
            risk=ActionRisk.A1_LOW,
            confirmation_policy=ConfirmationPolicy.PREAUTHORIZED,
            arguments_model=HomeTargetArgs,
            action="turn_off",
            compensation_action_id="home.switch.turn_on",
        ),
        _home_action(
            action_id="home.climate.set_temperature",
            label="设置空调温度",
            description="设置后台已授权空调的目标温度，每次执行都需要用户确认。",
            risk=ActionRisk.A2_CONFIRM,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            arguments_model=HomeTemperatureArgs,
            action="set_temperature",
        ),
        _home_action(
            action_id="home.light.set_brightness",
            label="设置灯光亮度",
            description="把后台已授权灯光设置为指定百分比亮度。",
            risk=ActionRisk.A1_LOW,
            confirmation_policy=ConfirmationPolicy.PREAUTHORIZED,
            arguments_model=HomeBrightnessArgs,
            action="set_brightness",
        ),
        _home_action(
            action_id="home.media.play",
            label="播放媒体",
            description="让后台已授权的媒体播放器继续播放。",
            risk=ActionRisk.A1_LOW,
            confirmation_policy=ConfirmationPolicy.PREAUTHORIZED,
            arguments_model=HomeTargetArgs,
            action="play",
        ),
        _home_action(
            action_id="home.media.pause",
            label="暂停媒体",
            description="暂停后台已授权的媒体播放器。",
            risk=ActionRisk.A1_LOW,
            confirmation_policy=ConfirmationPolicy.PREAUTHORIZED,
            arguments_model=HomeTargetArgs,
            action="pause",
        ),
        _home_action(
            action_id="home.media.set_volume",
            label="设置媒体音量",
            description="设置后台已授权媒体播放器的音量，每次执行都需要用户确认。",
            risk=ActionRisk.A2_CONFIRM,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            arguments_model=HomeVolumeArgs,
            action="volume_set",
        ),
        (
            ActionDefinition(
                action_id="desktop.notification.show",
                label="发送桌面通知",
                description="向用户已授权的在线桌面设备发送 L0/L1 系统通知。",
                risk=ActionRisk.A1_LOW,
                confirmation_policy=ConfirmationPolicy.PREAUTHORIZED,
                reversible=False,
                tool_name="desktop_notify",
                arguments_schema=DesktopNotifyArgs.model_json_schema(),
                timeout_seconds=12,
                verification_policy=VerificationPolicy.RECEIPT,
                verifier_id="device.command_receipt",
                max_privacy_level=PrivacyLevel.L1,
            ),
            DesktopNotifyArgs,
        ),
    ]
    for definition, arguments_model in registrations:
        registry.register(definition, arguments_model)
    registry.validate()
    return registry
