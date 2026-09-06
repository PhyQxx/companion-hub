"""FOCUS-01 聊天端专注工具：开始/停止/查看专注会话。

红线（docs/39「默认只建议，不自动拦截应用」）：工具只管理会话与查看
建议，不提供任何拦截能力。仅 L1 挂载（屏幕观察沉淀在 L0/L1，专注
建议也不进入 L2 私密会话）。
"""

from __future__ import annotations

from time import perf_counter
from typing import Annotated, cast

from pydantic import BaseModel, ConfigDict, Field

from app.focus.service import FocusService
from app.llm import ToolDefinition
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult


class FocusStartArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Annotated[str, Field(min_length=1, max_length=120)]
    keywords: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        default_factory=list, max_length=10
    )
    duration_minutes: Annotated[int, Field(ge=15, le=480)] = 60


class FocusStopArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pass


def _failure(tool_name: str, reason: str, started: float) -> ToolResult:
    return ToolResult(
        ok=False,
        tool_name=tool_name,
        reason_code=reason,
        latency_ms=(perf_counter() - started) * 1_000,
    )


class FocusStartTool:
    name = "focus_start"
    description = (
        "开始一个专注会话：声明专注目标与关键词（用于判断屏幕活动是否偏离），"
        "持续时间内守护服务只做建议不做拦截。用户新会话会替换旧会话。"
    )
    arguments_model: type[BaseModel] = FocusStartArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, service: FocusService) -> None:
        self._service = service

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=FocusStartArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(FocusStartArgs, arguments)
        if context.privacy_level != PrivacyLevel.L1:
            return _failure(self.name, "focus_requires_l1", started)
        if context.user_id is None:
            return _failure(self.name, "user_missing", started)
        session = self._service.start_session(
            str(context.user_id),
            target=args.target,
            keywords=list(args.keywords),
            duration_minutes=args.duration_minutes,
        )
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={
                "started": True,
                "session_id": session.session_id,
                "target": session.target,
                "keywords": list(session.target_keywords),
                "ends_at": session.ends_at.isoformat(),
                "note": "专注期间只建议不拦截；偏离目标或长时间工作时会温和提醒。",
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )


class FocusStopTool:
    name = "focus_stop"
    description = "停止当前专注会话。"
    arguments_model: type[BaseModel] = FocusStopArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, service: FocusService) -> None:
        self._service = service

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=FocusStopArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        if context.privacy_level != PrivacyLevel.L1:
            return _failure(self.name, "focus_requires_l1", started)
        if context.user_id is None:
            return _failure(self.name, "user_missing", started)
        stopped = self._service.stop_session(str(context.user_id))
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={"stopped": stopped is not None},
            latency_ms=(perf_counter() - started) * 1_000,
        )


class FocusStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pass


class FocusStatusTool:
    name = "focus_status"
    description = (
        "查看当前专注会话与基于最近屏幕观察的确定性建议（连续工作时长、"
        "主题切换、是否偏离目标）。只统计与建议，不展示屏幕原始内容。"
    )
    arguments_model: type[BaseModel] = FocusStatusArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, service: FocusService) -> None:
        self._service = service

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=FocusStatusArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        if context.privacy_level != PrivacyLevel.L1:
            return _failure(self.name, "focus_requires_l1", started)
        if context.user_id is None:
            return _failure(self.name, "user_missing", started)
        session = self._service.get_session(str(context.user_id))
        if session is None:
            return ToolResult(
                ok=True,
                tool_name=self.name,
                data={"active": False},
                latency_ms=(perf_counter() - started) * 1_000,
            )
        signals = await self._service.evaluate(session)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={
                "active": True,
                "session_id": session.session_id,
                "target": session.target,
                "ends_at": session.ends_at.isoformat(),
                "signals": [
                    {
                        "kind": signal.kind,
                        "message": signal.message,
                        "suggestion": signal.suggestion,
                    }
                    for signal in signals
                ],
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )


__all__ = ["FocusStartTool", "FocusStatusTool", "FocusStopTool"]
