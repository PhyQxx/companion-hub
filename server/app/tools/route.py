from __future__ import annotations

from time import perf_counter
from typing import Annotated, Literal, cast
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field

from app.llm import ToolDefinition

from .amap import AmapProvider, AmapProviderError
from .contracts import ToolContext, ToolResult
from .location import normalize_explicit, resolve_location
from .nearby import parse_route


class PlanRouteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    destination: Annotated[str, Field(min_length=1, max_length=200)]
    mode: Literal["walking", "driving", "transit"] = "walking"


class RouteTool:
    name = "plan_route"
    description = (
        "计算两个已确认地点之间的步行、驾车或公交路线概要。"
        "origin 可省略或传 'current_location';省略时自动使用设备定位,其次默认城市。"
    )
    arguments_model: type[BaseModel] = PlanRouteArgs

    def __init__(self, provider: AmapProvider) -> None:
        self._provider = provider

    def definition(self) -> ToolDefinition:
        return route_tool_definition()

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(PlanRouteArgs, arguments)
        try:
            origin = await resolve_location(
                self._provider,
                explicit=normalize_explicit(args.origin),
                ephemeral=context.ephemeral_location,
                default_city=context.default_city,
            )
            if not origin.gcj_location:
                raise AmapProviderError("tool_result_invalid")
            destination = await self._provider.geocode(
                args.destination, city=context.default_city
            )
            payload = await self._provider.route(
                origin.gcj_location,
                str(destination["location"]),
                mode=args.mode,
                origin_citycode=origin.citycode,
                destination_citycode=str(destination.get("citycode") or "") or None,
            )
            distance, duration, steps = parse_route(payload, args.mode)
            return ToolResult(
                ok=True,
                tool_name=self.name,
                provider="amap",
                latency_ms=(perf_counter() - started) * 1_000,
                data={
                    "provider": "amap",
                    "origin": origin.name,
                    "destination": str(
                        destination.get("formatted_address") or args.destination
                    ),
                    "mode": args.mode,
                    "distance_m": distance,
                    "duration_s": duration,
                    "steps": steps,
                    "navigation_uri": _navigation_uri(
                        origin.gcj_location,
                        str(destination["location"]),
                        args.destination,
                        args.mode,
                    ),
                },
                location_source=origin.source,
            )
        except AmapProviderError as error:
            return self._failure(error.reason_code, started)

    def _failure(self, reason_code: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            provider="amap",
            reason_code=reason_code,
            latency_ms=(perf_counter() - started) * 1_000,
        )


def _navigation_uri(origin: str, destination: str, name: str, mode: str) -> str:
    uri_mode = {"walking": "walk", "driving": "car", "transit": "bus"}[mode]
    return "https://uri.amap.com/navigation?" + urlencode(
        {
            "from": origin,
            "to": f"{destination},{name}",
            "mode": uri_mode,
            "src": "companion-hub",
            "coordinate": "gaode",
            "callnative": "0",
        }
    )


def route_tool_definition() -> ToolDefinition:
    return ToolDefinition(
        name=RouteTool.name,
        description=RouteTool.description,
        parameters=PlanRouteArgs.model_json_schema(),
    )
