"""HOME-01 聊天端场景工具：列出与手动运行场景。

红线：工具只能「运行场景」——运行展开为待确认计划，含 A2 动作的
计划必须经计划确认流执行。创建/修改场景走用户 API（保存前展示全部
动作与权限），聊天端不提供建场景工具，避免模型绕过用户审阅拼接
动作序列。
"""

from __future__ import annotations

from time import perf_counter
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.home_scene.service import HomeSceneService
from app.llm import ToolDefinition
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult


def _failure(tool_name: str, reason: str, started: float) -> ToolResult:
    return ToolResult(
        ok=False,
        tool_name=tool_name,
        reason_code=reason,
        latency_ms=(perf_counter() - started) * 1_000,
    )


class HomeSceneRunArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scene_id: UUID


class HomeSceneRunTool:
    name = "home_scene_run"
    description = (
        "手动运行一个已配置的家庭场景：展开为动作计划并返回计划 ID 与确认要求。"
        "含每次确认（A2）动作的计划必须等用户在计划确认流中明确同意后才执行。"
    )
    arguments_model: type[BaseModel] = HomeSceneRunArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, service: HomeSceneService) -> None:
        self._service = service

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=HomeSceneRunArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(HomeSceneRunArgs, arguments)
        if context.privacy_level != PrivacyLevel.L1:
            return _failure(self.name, "home_scene_requires_l1", started)
        if context.user_id is None:
            return _failure(self.name, "user_missing", started)
        try:
            triggered = await self._service.run_manual(context.user_id, args.scene_id)
        except LookupError:
            return _failure(self.name, "home_scene_not_found", started)
        except ValueError as error:
            return ToolResult(
                ok=False,
                tool_name=self.name,
                reason_code="invalid_home_scene",
                data={"reason_detail": str(error)},
                latency_ms=(perf_counter() - started) * 1_000,
            )
        if triggered is None:
            return _failure(self.name, "home_scene_not_found", started)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={
                "scene_id": str(triggered.scene_id),
                "scene_name": triggered.scene_name,
                "plan_id": str(triggered.plan_id),
                "plan_status": triggered.plan_status,
                "awaiting_confirmation": triggered.awaiting_confirmation,
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )


class HomeSceneListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pass


class HomeSceneListTool:
    name = "home_scene_list"
    description = "列出用户已配置的家庭场景（名称、触发器、生效时段与动作步骤数）。"
    arguments_model: type[BaseModel] = HomeSceneListArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, service: HomeSceneService) -> None:
        self._service = service

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=HomeSceneListArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        if context.privacy_level != PrivacyLevel.L1:
            return _failure(self.name, "home_scene_requires_l1", started)
        if context.user_id is None:
            return _failure(self.name, "user_missing", started)
        scenes = await self._service.list_scenes(context.user_id)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={
                "scenes": [
                    {
                        "id": str(scene.id),
                        "name": scene.name,
                        "trigger": scene.trigger,
                        "enabled": scene.enabled,
                        "window": (
                            f"{scene.window_start}-{scene.window_end}"
                            if scene.window_start and scene.window_end
                            else None
                        ),
                        "step_count": len(scene.steps),
                    }
                    for scene in scenes
                ]
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )


__all__ = ["HomeSceneListTool", "HomeSceneRunTool"]
