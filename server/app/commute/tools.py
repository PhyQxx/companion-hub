"""COMMUTE-01 聊天端出行工具：查下一个带地点的日程并给出出发建议。

仅 L1 挂载；amap 未配置或 tools.commute 未启用时 available=False。
出发提醒复用 TASK-01 调度（source_ref=commute:{event_id}），建议的
耗时来源、缓冲与出发时刻全部随结果透出，满足「建议来源和时间可解释」。
"""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from app.commute.service import DEFAULT_WITHIN_HOURS, CommuteRouteError, CommuteService
from app.llm import ToolDefinition
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult


class CommuteCheckArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    within_hours: int = Field(default=DEFAULT_WITHIN_HOURS, ge=1, le=72)


class CommuteCheckTool:
    name = "commute_check"
    description = (
        "查看用户下一个带地点的日程并给出出行建议：路线耗时（高德实时规划）、"
        "目的地天气与建议出发时刻（开始时间 - 耗时 - 缓冲），并自动设置出发提醒。"
        "用户问「接下来怎么出门/要不要出发了」时调用。"
    )
    arguments_model: type[BaseModel] = CommuteCheckArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, service_factory: Callable[[], CommuteService | None]) -> None:
        # 服务按需构建（含高德 provider），用完即关，模式与简报天气闭包一致。
        self._service_factory = service_factory

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=CommuteCheckArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(CommuteCheckArgs, arguments)
        # ToolContext 经 use_enum_values 校验后是普通字符串，必须用 != 比较
        if context.privacy_level != PrivacyLevel.L1:
            # L0 公开模式不读个人日程，L2 私密会话内容不入库（提醒任务不落库）。
            return self._failure("commute_requires_l1", started)
        if context.user_id is None:
            return self._failure("user_missing", started)
        service = self._service_factory()
        if service is None:
            return self._failure("commute_not_configured", started)
        try:
            event = await service.next_outing(context.user_id, within_hours=args.within_hours)
            if event is None:
                return ToolResult(
                    ok=True,
                    tool_name=self.name,
                    data={"has_outing": False},
                    latency_ms=(perf_counter() - started) * 1_000,
                )
            plan = await service.plan_commute(context.user_id, event)
        except CommuteRouteError as error:
            return ToolResult(
                ok=False,
                tool_name=self.name,
                reason_code=error.reason_code,
                latency_ms=(perf_counter() - started) * 1_000,
            )
        except Exception:
            return self._failure("calendar_unavailable", started)
        finally:
            await service.aclose()
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider="amap",
            data={"has_outing": True, **plan.to_payload()},
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1_000,
        )


__all__ = ["CommuteCheckTool"]
