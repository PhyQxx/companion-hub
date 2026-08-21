from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from app.tools import (
    AmapProvider,
    ClientLocation,
    ClientLocationPayload,
    GetWeatherArgs,
    SearchNearbyArgs,
    ToolContext,
    WeatherTool,
    wgs84_to_gcj02,
)

# 济南市区 WGS84 近似坐标。
_JINAN_WGS = (36.6657, 117.0195)


def _location_transport(
    request: httpx.Request,
) -> httpx.Response:
    path = request.url.path
    params = request.url.params
    if path == "/v3/geocode/regeo":
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "regeocode": {
                    "formatted_address": "山东省济南市历下区经十路123号",
                    "addressComponent": {
                        "province": "山东省",
                        "city": "济南市",
                        "district": "历下区",
                        "adcode": "370102",
                        "citycode": "0531",
                    },
                },
            },
        )
    if path == "/v3/geocode/geo":
        item = {
            "formatted_address": "山东省济南市",
            "adcode": "370100",
            "citycode": "0531",
            "location": "117.000000,36.650000",
        }
        return httpx.Response(
            200, json={"status": "1", "infocode": "10000", "geocodes": [item]}
        )
    if path == "/v3/weather/weatherInfo":
        adcode = params["city"]
        weather = "小雨" if adcode == "370102" else "多云"
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "lives": [
                    {
                        "weather": weather,
                        "temperature": "28",
                        "humidity": "70",
                        "winddirection": "东南",
                        "windpower": "3",
                        "reporttime": "2026-08-21 13:00:00",
                    }
                ],
                "forecasts": [
                    {
                        "reporttime": "2026-08-21 11:00:00",
                        "casts": [
                            {
                                "date": "2026-08-21",
                                "dayweather": "雷阵雨",
                                "nightweather": "晴",
                                "daytemp": "32",
                                "nighttemp": "24",
                            }
                        ],
                    }
                ],
            },
        )
    if path == "/v5/place/around":
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "pois": [
                    {
                        "id": "poi-1",
                        "name": "便民药店",
                        "address": "甲路1号",
                        "location": "117.021000,36.666000",
                        "distance": "300",
                    }
                ],
            },
        )
    if path == "/v5/direction/walking":
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "route": {
                    "paths": [
                        {
                            "distance": "420",
                            "cost": {"duration": "300"},
                            "steps": [{"instruction": "向东步行100米"}],
                        }
                    ]
                },
            },
        )
    raise AssertionError(f"unexpected path: {path}")


def _provider() -> tuple[AmapProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_location_transport),
        base_url="https://restapi.amap.com",
    )
    return AmapProvider("test-key", client=client), client


def _ephemeral(**overrides: object) -> ClientLocation:
    payload = {
        "latitude": _JINAN_WGS[0],
        "longitude": _JINAN_WGS[1],
        "accuracy_m": 30,
        "received_at": datetime.now(UTC),
    }
    payload.update(overrides)
    return ClientLocation.model_validate(payload)


def test_wgs84_to_gcj02_offsets_china_and_passes_through_abroad() -> None:
    latitude, longitude = _JINAN_WGS
    gcj_latitude, gcj_longitude = wgs84_to_gcj02(latitude, longitude)
    # 国测局偏移量级约数百米(0.001~0.02 度), 且不能是恒等变换。
    assert 0 < gcj_latitude - latitude < 0.02
    assert 0 < gcj_longitude - longitude < 0.02

    abroad = wgs84_to_gcj02(35.6762, 139.6503)  # 东京, 境外原样返回
    assert abroad == (35.6762, 139.6503)


def test_client_location_payload_rejects_out_of_range_coordinates() -> None:
    with pytest.raises(ValidationError):
        ClientLocationPayload(latitude=91, longitude=117)
    with pytest.raises(ValidationError):
        ClientLocationPayload(latitude=36, longitude=181)
    payload = ClientLocationPayload(latitude=36.6, longitude=117.0, accuracy_m=20)
    location = payload.to_client_location()
    assert location.is_fresh()
    assert not location.is_fresh(now=datetime.now(UTC) + timedelta(minutes=16))


async def test_weather_prefers_ephemeral_location_without_leaking_coordinates() -> None:
    provider, client = _provider()
    tool = WeatherTool(provider)
    try:
        result = await tool.execute(
            GetWeatherArgs(),
            ToolContext(
                privacy_level="L1",
                default_city="济南市",
                ephemeral_location=_ephemeral(),
            ),
        )
    finally:
        await client.aclose()

    assert result.ok is True
    assert result.location_source == "ephemeral"
    # regeo adcode=370102 命中"小雨", 证明走的是临时位置而不是默认城市。
    assert result.data["current"]["weather"] == "小雨"
    # 名称只到城市/区县精度, 不携带街道级 formatted_address。
    assert result.data["resolved_location"]["name"] == "山东省济南市历下区"
    serialized = json.dumps(result.data, ensure_ascii=False)
    assert "经十路" not in serialized
    # 原始 WGS84 坐标不得出现在回注给模型的数据里。
    assert str(_JINAN_WGS[0]) not in serialized
    assert str(_JINAN_WGS[1]) not in serialized


async def test_weather_explicit_location_beats_ephemeral_and_default_city() -> None:
    provider, client = _provider()
    tool = WeatherTool(provider)
    try:
        result = await tool.execute(
            GetWeatherArgs(location="济南市"),
            ToolContext(
                privacy_level="L1",
                default_city="济南市",
                ephemeral_location=_ephemeral(),
            ),
        )
    finally:
        await client.aclose()

    # 显式地点走 geocode(adcode=370100), 命中"多云", 不用临时位置。
    assert result.location_source == "explicit"
    assert result.data["current"]["weather"] == "多云"


async def test_stale_ephemeral_location_falls_back_to_default_city() -> None:
    provider, client = _provider()
    tool = WeatherTool(provider)
    stale = _ephemeral(received_at=datetime.now(UTC) - timedelta(minutes=20))
    try:
        result = await tool.execute(
            GetWeatherArgs(),
            ToolContext(
                privacy_level="L1",
                default_city="济南市",
                ephemeral_location=stale,
            ),
        )
    finally:
        await client.aclose()

    assert result.location_source == "default_city"
    assert result.data["current"]["weather"] == "多云"


async def test_nearby_uses_ephemeral_coordinates_as_search_origin() -> None:
    provider, client = _provider()
    from app.tools import NearbyTool

    try:
        tool = NearbyTool(provider)
        result = await tool.execute(
            SearchNearbyArgs(keyword="药店", category="pharmacy"),
            ToolContext(
                privacy_level="L1",
                ephemeral_location=_ephemeral(),
            ),
        )
    finally:
        await client.aclose()

    assert result.ok is True
    assert result.location_source == "ephemeral"
    assert result.data["resolved_origin"]["name"] == "山东省济南市历下区"
    assert result.data["resolved_origin"]["adcode"] == "370102"


async def test_current_location_literal_is_treated_as_unspecified() -> None:
    provider, client = _provider()
    tool = WeatherTool(provider)
    try:
        result = await tool.execute(
            GetWeatherArgs(location="current_location"),
            ToolContext(
                privacy_level="L1",
                default_city="济南市",
                ephemeral_location=_ephemeral(),
            ),
        )
    finally:
        await client.aclose()

    # "current_location" 字面量应走临时位置解析, 而不是拿去 geocode。
    assert result.location_source == "ephemeral"
    assert result.data["current"]["weather"] == "小雨"
