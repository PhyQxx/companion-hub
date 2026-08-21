from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.llm import ToolDefinition

from .amap import AmapProvider, AmapProviderError
from .contracts import ToolContext, ToolResult
from .location import normalize_explicit, resolve_location


class GetWeatherArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    location: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    mode: Literal["current", "forecast", "current_and_forecast"] = "current_and_forecast"
    forecast_days: Annotated[int, Field(ge=1, le=4)] = 2


class WeatherTool:
    name = "get_weather"
    description = (
        "查询中国大陆指定城市或区县的实时天气与未来四天内预报。"
        "location 可省略;省略时自动使用设备定位或默认城市,不要先反问用户。"
    )
    arguments_model: type[BaseModel] = GetWeatherArgs

    def __init__(self, provider: AmapProvider) -> None:
        self._provider = provider

    def definition(self) -> ToolDefinition:
        return weather_tool_definition()

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(GetWeatherArgs, arguments)
        try:
            resolved = await resolve_location(
                self._provider,
                explicit=normalize_explicit(args.location),
                ephemeral=context.ephemeral_location,
                default_city=context.default_city,
            )
            adcode = resolved.adcode
            data: dict[str, Any] = {
                "provider": "amap",
                "fetched_at": datetime.now(UTC).isoformat(),
                "resolved_location": {
                    "name": resolved.name,
                    "adcode": adcode,
                },
                "current": None,
                "forecast": [],
                "source_url": "https://www.amap.com/",
            }
            report_times: list[str] = []
            if args.mode in {"current", "current_and_forecast"}:
                live = await self._provider.weather(adcode, extensions="base")
                lives = live.get("lives")
                if not isinstance(lives, list) or not lives or not isinstance(lives[0], dict):
                    raise AmapProviderError("tool_result_invalid")
                item = lives[0]
                report_times.append(str(item.get("reporttime") or ""))
                data["current"] = {
                    "weather": item.get("weather"),
                    "temperature_c": _number(item.get("temperature")),
                    "humidity_percent": _number(item.get("humidity")),
                    "wind": _wind(item.get("winddirection"), item.get("windpower")),
                }
            if args.mode in {"forecast", "current_and_forecast"}:
                forecast = await self._provider.weather(adcode, extensions="all")
                forecasts = forecast.get("forecasts")
                if (
                    not isinstance(forecasts, list)
                    or not forecasts
                    or not isinstance(forecasts[0], dict)
                ):
                    raise AmapProviderError("tool_result_invalid")
                block = forecasts[0]
                report_times.append(str(block.get("reporttime") or ""))
                casts = block.get("casts")
                if not isinstance(casts, list):
                    raise AmapProviderError("tool_result_invalid")
                data["forecast"] = [
                    {
                        "date": item.get("date"),
                        "day_weather": item.get("dayweather"),
                        "night_weather": item.get("nightweather"),
                        "low_c": _number(item.get("nighttemp")),
                        "high_c": _number(item.get("daytemp")),
                    }
                    for item in casts[: args.forecast_days]
                    if isinstance(item, dict)
                ]
            data["report_time"] = max((item for item in report_times if item), default=None)
            return ToolResult(
                ok=True,
                tool_name=self.name,
                provider="amap",
                data=data,
                latency_ms=(perf_counter() - started) * 1_000,
                location_source=resolved.source,
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


def _number(value: Any) -> int | float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _wind(direction: Any, power: Any) -> str | None:
    if direction in {None, ""} and power in {None, ""}:
        return None
    return f"{direction or ''}风 {power or ''}级".strip()


def weather_tool_definition() -> ToolDefinition:
    return ToolDefinition(
        name=WeatherTool.name,
        description=WeatherTool.description,
        parameters=GetWeatherArgs.model_json_schema(),
    )
