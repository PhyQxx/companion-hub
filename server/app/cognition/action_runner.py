from __future__ import annotations

from uuid import UUID

from app.home_assistant.models import HomeAssistantError
from app.home_assistant.tools import HomeStateProvider
from app.llm import ToolCall
from app.schemas import PrivacyLevel
from app.tools import ToolContext, ToolExecutor, ToolResult

from .action_plan import ActionRunResult, ActionStepView, ActionVerificationStatus


class ToolActionRunner:
    """Run only the precompiled tool call stored in a confirmed action step."""

    def __init__(
        self,
        executor: ToolExecutor,
        *,
        home_state_provider: HomeStateProvider | None = None,
    ) -> None:
        self._executor = executor
        self._home_state_provider = home_state_provider

    async def __call__(self, step: ActionStepView, user_id: UUID) -> ActionRunResult:
        privacy_level = PrivacyLevel.L2
        if step.tool_name == "desktop_notify":
            declared_privacy = step.tool_arguments.get("privacy_level")
            if declared_privacy not in {"L0", "L1"}:
                return ActionRunResult(
                    execution=ToolResult(
                        ok=False,
                        tool_name=step.tool_name,
                        reason_code="notification_privacy_level_invalid",
                        latency_ms=0,
                    )
                )
            privacy_level = PrivacyLevel(str(declared_privacy))
        elif step.tool_name == "mcp_tool_call":
            # MCP-D：外部 MCP 服务不接收 L2 内容，动作定义已限 L1
            privacy_level = PrivacyLevel.L1
        execution = await self._executor.execute(
            ToolCall(
                id=f"action-step-{step.id}",
                function={
                    "name": step.tool_name,
                    "arguments": step.tool_arguments,
                },
            ),
            ToolContext(
                privacy_level=privacy_level,
                user_id=user_id,
                user_text="确认执行行动计划",
                idempotency_key=step.idempotency_key,
            ),
        )
        if not execution.result.ok:
            return ActionRunResult(execution=execution.result)
        if step.verification_policy == "receipt":
            if step.verifier_id == "mcp.call_receipt":
                # MCP-D：调用回执即证据（server/tool/ok），远端正文不进验证记录
                evidence = {
                    key: value
                    for key, value in execution.result.data.items()
                    if key in {"server_id", "tool_name", "ok"}
                }
                return ActionRunResult(
                    execution=execution.result,
                    verification_status=ActionVerificationStatus.VERIFIED,
                    verification_result=evidence,
                )
            if step.verifier_id != "device.command_receipt":
                return ActionRunResult(
                    execution=execution.result,
                    verification_status=ActionVerificationStatus.INCONCLUSIVE,
                    verification_reason_code="action_verifier_unavailable",
                )
            evidence = {
                key: value
                for key, value in execution.result.data.items()
                if key in {"command_id", "device_id", "status"}
            }
            return ActionRunResult(
                execution=execution.result,
                verification_status=ActionVerificationStatus.VERIFIED,
                verification_result=evidence,
            )
        if step.verification_policy != "read_after_write":
            return ActionRunResult(execution=execution.result)
        provider = self._home_state_provider
        if step.verifier_id != "home.entity_state" or provider is None:
            return ActionRunResult(
                execution=execution.result,
                verification_status=ActionVerificationStatus.INCONCLUSIVE,
                verification_reason_code="action_verifier_unavailable",
            )
        return self._verify_home_state(step, execution.result, provider)

    def _verify_home_state(
        self,
        step: ActionStepView,
        execution: ToolResult,
        provider: HomeStateProvider,
    ) -> ActionRunResult:
        target = step.tool_arguments.get("target")
        action = step.tool_arguments.get("action")
        if not isinstance(target, str) or not isinstance(action, str):
            return ActionRunResult(
                execution=execution,
                verification_status=ActionVerificationStatus.INCONCLUSIVE,
                verification_reason_code="action_verification_arguments_invalid",
            )
        try:
            policy = provider.resolve(target)
            state = provider.get_state(policy.entity_id)
        except HomeAssistantError as error:
            return ActionRunResult(
                execution=execution,
                verification_status=ActionVerificationStatus.INCONCLUSIVE,
                verification_reason_code=error.reason_code,
            )

        evidence: dict[str, object] = {"entity_id": policy.entity_id}
        matched = False
        if action in {"turn_on", "turn_off"}:
            expected = "on" if action == "turn_on" else "off"
            evidence.update(expected_state=expected, observed_state=state.state)
            matched = state.state == expected
        elif action == "set_temperature":
            expected_temperature = step.tool_arguments.get("temperature_c")
            observed_temperature = state.attributes.get("temperature")
            evidence.update(
                expected_temperature_c=expected_temperature,
                observed_temperature_c=observed_temperature,
            )
            matched = (
                isinstance(expected_temperature, int | float)
                and isinstance(observed_temperature, int | float)
                and abs(float(expected_temperature) - float(observed_temperature)) <= 0.1
            )
        elif action == "set_brightness":
            expected_pct = step.tool_arguments.get("brightness_pct")
            observed_raw = state.attributes.get("brightness")
            observed_pct = (
                round(float(observed_raw) * 100 / 255)
                if isinstance(observed_raw, int | float)
                else None
            )
            evidence.update(
                expected_brightness_pct=expected_pct,
                observed_brightness_pct=observed_pct,
            )
            matched = (
                isinstance(expected_pct, int | float)
                and observed_pct is not None
                and abs(float(expected_pct) - observed_pct) <= 1
            )
        elif action in {"play", "pause"}:
            expected_state = "playing" if action == "play" else "paused"
            evidence.update(expected_state=expected_state, observed_state=state.state)
            matched = state.state == expected_state
        elif action == "volume_set":
            expected_level = step.tool_arguments.get("volume_level")
            observed_level = state.attributes.get("volume_level")
            evidence.update(
                expected_volume_level=expected_level,
                observed_volume_level=observed_level,
            )
            matched = (
                isinstance(expected_level, int | float)
                and isinstance(observed_level, int | float)
                and abs(float(expected_level) - float(observed_level)) <= 0.01
            )
        return ActionRunResult(
            execution=execution,
            verification_status=(
                ActionVerificationStatus.VERIFIED
                if matched
                else ActionVerificationStatus.INCONCLUSIVE
            ),
            verification_result=evidence,
            verification_reason_code=(None if matched else "action_state_mismatch_unknown_outcome"),
        )
