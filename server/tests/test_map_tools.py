from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.tools import (
    AmapProvider,
    AmapProviderError,
    NearbyTool,
    PlanRouteArgs,
    RouteTool,
    SearchNearbyArgs,
    ToolContext,
)


def _map_transport(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    params = request.url.params
    if path == "/v3/geocode/geo":
        address = params["address"]
        if "医院" in address:
            item = {
                "formatted_address": "济南大康民世医院",
                "adcode": "370112",
                "citycode": "0531",
                "location": "117.130000,36.690000",
            }
        else:
            item = {
                "formatted_address": "济南市历城区璟樾花园",
                "adcode": "370112",
                "citycode": "0531",
                "location": "117.120000,36.680000",
            }
        return httpx.Response(
            200,
            json={"status": "1", "infocode": "10000", "geocodes": [item]},
        )
    if path == "/v5/place/around":
        assert params["types"] == "090000"
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "pois": [
                    {
                        "id": "poi-far",
                        "name": "直线较近医院",
                        "address": "甲路1号",
                        "location": "117.121000,36.681000",
                        "distance": "500",
                    },
                    {
                        "id": "poi-near",
                        "name": "步行较近医院",
                        "address": "乙路2号",
                        "location": "117.122000,36.682000",
                        "distance": "700",
                    },
                ],
            },
        )
    if path == "/v5/direction/walking":
        destination = params["destination"]
        distance = "1200" if destination.startswith("117.121") else "900"
        return _route_response(distance=distance, duration="720")
    if path == "/v5/direction/driving":
        return _route_response(distance="1800", duration="300")
    raise AssertionError(f"unexpected path: {path}")


def _route_response(*, distance: str, duration: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "1",
            "infocode": "10000",
            "route": {
                "paths": [
                    {
                        "distance": distance,
                        "cost": {"duration": duration},
                        "steps": [
                            {"instruction": "向东步行100米"},
                            {"instruction": "到达目的地"},
                        ],
                    }
                ]
            },
        },
    )


def _tools() -> tuple[NearbyTool, RouteTool, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_map_transport),
        base_url="https://restapi.amap.com",
    )
    provider = AmapProvider("test-key", client=client)
    return NearbyTool(provider), RouteTool(provider), client


async def test_nearby_hospital_is_ranked_by_verified_walking_route() -> None:
    nearby, _, client = _tools()
    try:
        result = await nearby.execute(
            SearchNearbyArgs(
                origin="璟樾花园",
                keyword="医院",
                category="hospital",
                rank_by="walking_distance",
            ),
            ToolContext(privacy_level="L1", default_city="济南市"),
        )
    finally:
        await client.aclose()

    assert result.ok is True
    results = result.data["results"]
    assert isinstance(results, list)
    assert [item["name"] for item in results] == ["步行较近医院", "直线较近医院"]
    assert results[0]["distance_m"] == 900
    assert results[0]["distance_basis"] == "walking_route"
    assert results[0]["navigation_uri"].startswith("https://uri.amap.com/marker?")


async def test_route_tool_returns_bounded_route_summary() -> None:
    _, route, client = _tools()
    try:
        result = await route.execute(
            PlanRouteArgs(
                origin="璟樾花园",
                destination="济南大康民世医院",
                mode="walking",
            ),
            ToolContext(privacy_level="L1", default_city="济南市"),
        )
    finally:
        await client.aclose()

    assert result.ok is True
    assert result.data["distance_m"] == 900
    assert result.data["duration_s"] == 720
    assert result.data["steps"] == ["向东步行100米", "到达目的地"]
    assert result.data["navigation_uri"].startswith("https://uri.amap.com/navigation?")
    navigation_query = parse_qs(urlparse(result.data["navigation_uri"]).query)
    assert "from" not in navigation_query
    assert navigation_query["to"][0].startswith("117.130000,36.690000")


async def test_nearby_requires_explicit_origin_when_no_default_city() -> None:
    nearby, _, client = _tools()
    try:
        result = await nearby.execute(
            SearchNearbyArgs(keyword="医院", category="hospital"),
            ToolContext(privacy_level="L1"),
        )
    finally:
        await client.aclose()

    assert result.reason_code == "location_required"


async def test_provider_ttl_cache_is_shared_without_plaintext_request_keys() -> None:
    calls = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"status": "1", "infocode": "10000", "lives": []},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    first = AmapProvider(
        "cache-test-key",
        base_url="https://cache-test.amap.invalid",
        client=client,
        cache_namespace="provider-cache-test",
    )
    second = AmapProvider(
        "cache-test-key",
        base_url="https://cache-test.amap.invalid",
        client=client,
        cache_namespace="provider-cache-test",
    )
    try:
        await first.weather("370100", extensions="base")
        await second.weather("370100", extensions="base")
    finally:
        await client.aclose()

    assert calls == 1
    assert second.cache_hits == 1
    assert all("370100" not in key for key in AmapProvider._cache)


async def test_geocode_ambiguity_returns_three_safe_candidates() -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "geocodes": [
                    {
                        "formatted_address": f"候选地点{i}",
                        "adcode": f"37010{i}",
                        "citycode": "0531",
                        "location": f"117.{i},36.{i}",
                    }
                    for i in range(4)
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    provider = AmapProvider(
        "ambiguity-test-key",
        base_url="https://ambiguity-test.amap.invalid",
        client=client,
    )
    try:
        with pytest.raises(AmapProviderError) as captured:
            await provider.geocode("同名地点")
    finally:
        await client.aclose()

    assert captured.value.reason_code == "location_ambiguous"
    assert captured.value.candidates == [
        {"name": "候选地点0", "adcode": "370100", "citycode": "0531"},
        {"name": "候选地点1", "adcode": "370101", "citycode": "0531"},
        {"name": "候选地点2", "adcode": "370102", "citycode": "0531"},
    ]
