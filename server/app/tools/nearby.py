from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field

from app.llm import ToolDefinition

from .amap import AmapProvider, AmapProviderError
from .contracts import ToolContext, ToolResult
from .location import normalize_explicit, resolve_location

_CATEGORY_TYPES = {
    "hospital": "090000",
    "pharmacy": "090601",
    "restaurant": "050000",
    "charging_station": "011100",
    "supermarket": "060400",
    "park": "110101",
    "custom": "",
}


class SearchNearbyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    keyword: Annotated[str, Field(min_length=1, max_length=80)]
    category: Literal[
        "hospital",
        "pharmacy",
        "restaurant",
        "charging_station",
        "supermarket",
        "park",
        "custom",
    ] = "custom"
    radius_m: Annotated[int, Field(ge=500, le=20_000)] = 5_000
    limit: Annotated[int, Field(ge=1, le=10)] = 5
    rank_by: Literal[
        "straight_line", "walking_distance", "driving_distance"
    ] = "walking_distance"


class NearbyTool:
    name = "search_nearby"
    description = (
        "查询指定地点附近的医院、药店、餐厅、充电站等公开地点, 并按距离排序。"
        "origin 可省略或传 'current_location';省略时自动使用设备定位,其次默认城市。"
    )
    arguments_model: type[BaseModel] = SearchNearbyArgs

    def __init__(self, provider: AmapProvider) -> None:
        self._provider = provider

    def definition(self) -> ToolDefinition:
        return nearby_tool_definition()

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(SearchNearbyArgs, arguments)
        try:
            origin = await resolve_location(
                self._provider,
                explicit=normalize_explicit(args.origin),
                ephemeral=context.ephemeral_location,
                default_city=context.default_city,
            )
            origin_location = origin.gcj_location
            if not origin_location:
                raise AmapProviderError("tool_result_invalid")
            payload = await self._provider.search_around(
                origin_location,
                keywords=args.keyword,
                types=_CATEGORY_TYPES[args.category],
                radius_m=args.radius_m,
                limit=min(10, max(args.limit, 5)),
                region=origin.adcode or None,
            )
            raw_pois = payload.get("pois")
            if not isinstance(raw_pois, list):
                raise AmapProviderError("tool_result_invalid")
            candidates = [
                _poi(item, args.category)
                for item in raw_pois[:10]
                if isinstance(item, dict) and item.get("name") and item.get("location")
            ]
            if not candidates:
                return self._failure("no_results", started)
            if args.rank_by != "straight_line":
                await self._verify_route_distances(
                    candidates,
                    origin_location,
                    mode="walking" if args.rank_by == "walking_distance" else "driving",
                )
            candidates.sort(
                key=lambda item: (
                    0
                    if args.rank_by == "straight_line"
                    or item["distance_basis"] != "straight_line"
                    else 1,
                    cast(int, item["distance_m"]),
                )
            )
            return ToolResult(
                ok=True,
                tool_name=self.name,
                provider="amap",
                latency_ms=(perf_counter() - started) * 1_000,
                data={
                    "provider": "amap",
                    "resolved_origin": {
                        "name": origin.name,
                        "adcode": origin.adcode,
                    },
                    "query_radius_m": args.radius_m,
                    "rank_by": args.rank_by,
                    "results": candidates[: args.limit],
                },
                location_source=origin.source,
            )
        except AmapProviderError as error:
            return self._failure(error.reason_code, started)

    async def _verify_route_distances(
        self,
        candidates: list[dict[str, Any]],
        origin: str,
        *,
        mode: Literal["walking", "driving"],
    ) -> None:
        selected = candidates if mode == "walking" else candidates[:3]

        async def verify(item: dict[str, Any]) -> None:
            try:
                payload = await self._provider.route(
                    origin,
                    str(item["location"]),
                    mode=mode,
                )
                distance, duration, _ = parse_route(payload, mode)
                item["distance_m"] = distance
                item["duration_s"] = duration
                item["distance_basis"] = f"{mode}_route"
            except AmapProviderError:
                item["distance_basis"] = "straight_line_fallback"

        await asyncio.gather(*(verify(item) for item in selected))

    def _failure(self, reason_code: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            provider="amap",
            reason_code=reason_code,
            latency_ms=(perf_counter() - started) * 1_000,
        )


def _poi(item: dict[str, Any], category: str) -> dict[str, Any]:
    location = str(item["location"])
    name = str(item["name"])
    return {
        "name": name,
        "address": str(item.get("address") or ""),
        "poi_id": str(item.get("id") or ""),
        "category": category,
        "distance_m": _integer(item.get("distance")) or 0,
        "duration_s": None,
        "distance_basis": "straight_line",
        "location": location,
        "navigation_uri": _marker_uri(location, name),
    }


def _marker_uri(location: str, name: str) -> str:
    return "https://uri.amap.com/marker?" + urlencode(
        {"position": location, "name": name, "src": "companion-hub", "callnative": "0"}
    )


def _integer(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_route(
    payload: dict[str, Any], mode: str
) -> tuple[int, int | None, list[str]]:
    route = payload.get("route")
    if not isinstance(route, dict):
        raise AmapProviderError("route_unavailable")
    choices = route.get("transits" if mode == "transit" else "paths")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AmapProviderError("route_unavailable")
    choice = choices[0]
    distance = _integer(choice.get("distance"))
    if distance is None:
        raise AmapProviderError("tool_result_invalid")
    cost = choice.get("cost")
    duration = _integer(cost.get("duration")) if isinstance(cost, dict) else None
    if duration is None:
        duration = _integer(choice.get("duration"))
    steps: list[str] = []
    raw_steps = choice.get("steps")
    if isinstance(raw_steps, list):
        steps = [
            str(item["instruction"])
            for item in raw_steps[:12]
            if isinstance(item, dict) and item.get("instruction")
        ]
    return distance, duration, steps


def nearby_tool_definition() -> ToolDefinition:
    return ToolDefinition(
        name=NearbyTool.name,
        description=NearbyTool.description,
        parameters=SearchNearbyArgs.model_json_schema(),
    )
