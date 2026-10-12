"""HRM 工具层：hermes_status（只读挂载）与 hermes_delegate（计划专用）。

hermes_delegate 与 mcp_tool_call 同一模式：不挂载为聊天工具
（chat/service._device_tool_ready 恒 False），唯一入口是 Action Registry
编译出的已确认计划步骤；执行仅入队 Job 返回回执，不在回合内等待。
"""

from __future__ import annotations

from time import perf_counter
from typing import Annotated, Any, cast

from pydantic import BaseModel, ConfigDict, Field

from app.ids import uuid7
from app.integrations.hermes import HermesManager, HermesManagerError
from app.llm import ToolDefinition
from app.schemas import PrivacyLevel

from .contracts import ToolContext, ToolResult

HERMES_DELEG_KIND = "deleg.hermes_task"


class HermesStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HermesStatusTool:
    name = "hermes_status"
    description = (
        "查询 NAS 上 hermes 服务器代理的连接状态、版本与今日任务额度。"
        "只读，不含配置细节。"
    )
    arguments_model: type[BaseModel] = HermesStatusArgs
    runs_local = False
    max_privacy_level = PrivacyLevel.L1

    def __init__(self, manager: HermesManager) -> None:
        self._manager = manager

    @property
    def available(self) -> bool:
        return True

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=HermesStatusArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        try:
            config = await self._manager.aconfig()
        except Exception:
            return self._failure("hermes_unavailable", started)
        if not config.enabled:
            return self._failure("hermes_disabled", started)
        health = await self._manager.probe()
        if health.status != "ok":
            return self._failure(health.reason_code or "hermes_unreachable", started)
        state = self._manager.state()
        today_total = sum(state.daily_counts.values())
        data: dict[str, Any] = {
            "status": health.status,
            "version": health.version,
            "today_runs": today_total,
            "daily_limit": config.daily_run_limit,
            "pending_approval_hint": any(
                entry.get("status") == "waiting_for_approval"
                for entry in state.recent_runs
            ),
        }
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider="hermes",
            latency_ms=(perf_counter() - started) * 1_000,
            data=data,
        )

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            admission_status="not_admitted",
            ok=False,
            tool_name=self.name,
            provider="hermes",
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1_000,
        )


class HermesDelegateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instruction: Annotated[str, Field(min_length=1, max_length=8_000)]
    timeout_seconds: Annotated[int, Field(ge=60, le=3_600)] | None = None


class HermesDelegateTool:
    """行动计划专用入口：入队 deleg.hermes_task Job，立即返回回执。"""

    name = "hermes_delegate"
    description = (
        "把自然语言任务委派给 NAS 上的 hermes 服务器代理后台执行"
        "（终端/文件/浏览器等，受限用户权限），完成后主动汇报。"
        "仅经用户确认的行动计划触发。"
    )
    arguments_model: type[BaseModel] = HermesDelegateArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L1

    def __init__(self, manager: HermesManager, engine: Any) -> None:
        self._manager = manager
        self._engine = engine

    @property
    def available(self) -> bool:
        return True

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=HermesDelegateArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(HermesDelegateArgs, arguments)
        if context.privacy_level != PrivacyLevel.L1:
            return self._failure("private_session_unsupported", started)
        if context.user_id is None:
            return self._failure("user_context_missing", started)
        try:
            config = await self._manager.aconfig()
        except Exception:
            return self._failure("hermes_unavailable", started)
        if not config.enabled:
            return self._failure("hermes_disabled", started)
        instruction = args.instruction.strip()
        if not instruction or len(instruction) > config.instruction_max_chars:
            return self._failure("hermes_instruction_too_long", started)
        try:
            self._manager.check_quota(context.user_id, config)
        except HermesManagerError as error:
            return self._failure(error.reason_code, started)
        payload: dict[str, Any] = {
            "user_id": str(context.user_id),
            "instruction": instruction,
        }
        if context.turn_id is not None:
            payload["turn_id"] = str(context.turn_id)
        if args.timeout_seconds is not None:
            payload["timeout_seconds"] = min(args.timeout_seconds, config.run_max_seconds)
        idempotency_key = (
            f"hermes-{context.idempotency_key}"
            if context.idempotency_key
            else f"hermes-{uuid7()}"
        )
        job = await self._engine.submit(
            HERMES_DELEG_KIND,
            payload,
            idempotency_key=idempotency_key,
            owner=str(context.user_id),
            source_turn_id=context.turn_id,
            resource_class="deleg",
            max_attempts=2,
        )
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider="job-engine",
            latency_ms=(perf_counter() - started) * 1_000,
            data={
                "job_id": str(job.id),
                "kind": HERMES_DELEG_KIND,
                "status": job.status,
                "ack": "任务已转给 hermes 后台执行，完成后我会主动告诉你结果。",
            },
        )

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            admission_status="not_admitted",
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1_000,
        )
