from __future__ import annotations

import json
from typing import Any

import httpx

from app.llm import ToolCall
from app.tools import (
    AmapProvider,
    GetWeatherArgs,
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    WeatherTool,
)


def _amap_transport(request: httpx.Request) -> httpx.Response:
    assert request.url.params["key"] == "test-key"
    if request.url.path == "/v3/geocode/geo":
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "geocodes": [
                    {
                        "formatted_address": "山东省济南市历城区",
                        "adcode": "370112",
                        "location": "117.120000,36.680000",
                    }
                ],
            },
        )
    if request.url.params["extensions"] == "base":
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "lives": [
                    {
                        "weather": "多云",
                        "temperature": "29",
                        "humidity": "62",
                        "winddirection": "南",
                        "windpower": "2",
                        "reporttime": "2026-08-21 11:00:00",
                    }
                ],
            },
        )
    return httpx.Response(
        200,
        json={
            "status": "1",
            "infocode": "10000",
            "forecasts": [
                {
                    "reporttime": "2026-08-21 11:00:00",
                    "casts": [
                        {
                            "date": "2026-08-21",
                            "dayweather": "多云",
                            "nightweather": "晴",
                            "nighttemp": "27",
                            "daytemp": "33",
                        }
                    ],
                }
            ],
        },
    )


def _weather_tool(
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[WeatherTool, AmapProvider]:
    client = httpx.AsyncClient(
        transport=transport or httpx.MockTransport(_amap_transport),
        base_url="https://restapi.amap.com",
    )
    provider = AmapProvider("test-key", client=client)
    return WeatherTool(provider), provider


async def test_weather_tool_returns_structured_current_and_forecast() -> None:
    tool, provider = _weather_tool()
    try:
        result = await tool.execute(
            GetWeatherArgs(location="济南市历城区", forecast_days=1),
            ToolContext(privacy_level="L1"),
        )
    finally:
        await provider._client.aclose()

    assert result.ok is True
    assert result.provider == "amap"
    assert result.data["resolved_location"] == {
        "name": "山东省济南市历城区",
        "adcode": "370112",
    }
    assert result.data["current"] == {
        "weather": "多云",
        "temperature_c": 29,
        "humidity_percent": 62,
        "wind": "南风 2级",
    }
    forecast = result.data["forecast"]
    assert isinstance(forecast, list) and forecast[0]["high_c"] == 33


async def test_weather_uses_default_city_and_requires_a_location() -> None:
    tool, provider = _weather_tool()
    try:
        success = await tool.execute(
            GetWeatherArgs(mode="current"),
            ToolContext(privacy_level="L1", default_city="济南市"),
        )
        failure = await tool.execute(
            GetWeatherArgs(mode="current"),
            ToolContext(privacy_level="L1"),
        )
    finally:
        await provider._client.aclose()

    assert success.ok is True
    assert failure.reason_code == "location_required"


async def test_executor_blocks_l2_before_provider_call() -> None:
    calls = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _amap_transport(request)

    tool, provider = _weather_tool(httpx.MockTransport(transport))
    executor = ToolExecutor(ToolRegistry([tool]))
    call = ToolCall(
        id="call-1",
        function={"name": "get_weather", "arguments": {"location": "济南市"}},
    )
    try:
        execution = await executor.execute(call, ToolContext(privacy_level="L2"))
    finally:
        await provider._client.aclose()

    assert execution.result.reason_code == "egress_blocked"
    assert calls == 0


async def test_amap_business_error_maps_without_leaking_provider_info() -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"status": "0", "infocode": "10001", "info": "INVALID_USER_KEY"},
        )

    tool, provider = _weather_tool(httpx.MockTransport(transport))
    try:
        result = await tool.execute(
            GetWeatherArgs(location="济南市", mode="current"),
            ToolContext(privacy_level="L1"),
        )
    finally:
        await provider._client.aclose()

    assert result.reason_code == "provider_auth_failed"
    assert "INVALID_USER_KEY" not in json.dumps(result.model_dump(mode="json"))


def test_weather_schema_rejects_provider_parameters() -> None:
    schema: dict[str, Any] = WeatherTool.definition(WeatherTool.__new__(WeatherTool)).parameters
    assert schema["additionalProperties"] is False
    assert "key" not in schema["properties"]
