"""get_location 工具：回答“我在哪/当前位置”类问题(docs/05 地图/天气章节 §6)。

复用位置解析链,但只走临时位置(设备定位)与默认城市两级,不做显式地点 geocode。
临时位置经 _from_regeo 只回注到城市/区县精度;坐标不出解析链,也不入结果。
"""

from __future__ import annotations

from time import perf_counter

from pydantic import BaseModel, ConfigDict

from app.llm import ToolDefinition

from .amap import AmapProvider, AmapProviderError
from .contracts import ToolContext, ToolResult
from .location import resolve_location


class GetLocationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocationTool:
    name = "get_location"
    description = (
        "查询用户当前所在的城市/区县,用于回答“我在哪”“现在在什么位置”类问题;"
        "不返回精确坐标。无需参数;优先使用设备定位,没有时回落默认城市,"
        "返回的 source 字段标明来源(ephemeral=设备定位,default_city=默认城市,可能不准)。"
    )
    arguments_model: type[BaseModel] = GetLocationArgs

    def __init__(self, provider: AmapProvider) -> None:
        self._provider = provider

    def definition(self) -> ToolDefinition:
        return location_tool_definition()

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        cache_hits_before = self._provider.cache_hits
        try:
            resolved = await resolve_location(
                self._provider,
                explicit=None,
                ephemeral=context.ephemeral_location,
                default_city=context.default_city,
            )
        except AmapProviderError as error:
            return ToolResult(
                ok=False,
                tool_name=self.name,
                provider="amap",
                reason_code=error.reason_code,
                latency_ms=(perf_counter() - started) * 1_000,
            )
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider="amap",
            latency_ms=(perf_counter() - started) * 1_000,
            cache_hit=self._provider.cache_hits > cache_hits_before,
            data={
                "name": resolved.name,
                "adcode": resolved.adcode,
                "source": resolved.source,
            },
            location_source=resolved.source,
        )


def location_tool_definition() -> ToolDefinition:
    return ToolDefinition(
        name=LocationTool.name,
        description=LocationTool.description,
        parameters=GetLocationArgs.model_json_schema(),
    )
