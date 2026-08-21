from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Mapping
from time import monotonic
from typing import Any

import httpx


class AmapProviderError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class AmapProvider:
    """Bounded client for the fixed Amap Web Service API surface."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://restapi.amap.com",
        timeout_ms: int = 3_500,
        max_retries: int = 1,
        max_concurrency: int = 2,
        requests_per_minute: int = 30,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("amap api key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_ms / 1_000
        self._max_retries = max_retries
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._requests_per_minute = requests_per_minute
        self._request_times: deque[float] = deque()
        self._rate_lock = asyncio.Lock()
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def geocode(self, address: str, *, city: str | None = None) -> dict[str, Any]:
        params = {"address": address}
        if city:
            params["city"] = city
        payload = await self._request("/v3/geocode/geo", params)
        geocodes = payload.get("geocodes")
        if not isinstance(geocodes, list) or not geocodes:
            raise AmapProviderError("location_not_found")
        if len(geocodes) > 1:
            raise AmapProviderError("location_ambiguous")
        item = geocodes[0]
        if not isinstance(item, dict) or not item.get("adcode") or not item.get("location"):
            raise AmapProviderError("tool_result_invalid")
        return item

    async def weather(self, adcode: str, *, extensions: str) -> dict[str, Any]:
        return await self._request(
            "/v3/weather/weatherInfo",
            {"city": adcode, "extensions": extensions},
        )

    async def regeo(self, location: str) -> dict[str, Any]:
        """逆地理编码:GCJ-02 "lng,lat" → regeocode 地址组件(含 adcode)。"""
        payload = await self._request(
            "/v3/geocode/regeo",
            {"location": location, "extensions": "base"},
        )
        regeocode = payload.get("regeocode")
        if not isinstance(regeocode, dict):
            raise AmapProviderError("tool_result_invalid")
        return regeocode

    async def search_around(
        self,
        location: str,
        *,
        keywords: str,
        types: str,
        radius_m: int,
        limit: int,
        region: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "location": location,
            "keywords": keywords,
            "types": types,
            "radius": str(radius_m),
            "sortrule": "distance",
            "page_size": str(limit),
            "page_num": "1",
        }
        if region:
            params["region"] = region
            params["city_limit"] = "true"
        return await self._request("/v5/place/around", params)

    async def route(
        self,
        origin: str,
        destination: str,
        *,
        mode: str,
        origin_citycode: str | None = None,
        destination_citycode: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "origin": origin,
            "destination": destination,
            "show_fields": "cost",
        }
        if mode == "driving":
            params["strategy"] = "32"
            path = "/v5/direction/driving"
        elif mode == "walking":
            path = "/v5/direction/walking"
        elif mode == "transit":
            if not origin_citycode or not destination_citycode:
                raise AmapProviderError("route_citycode_required")
            params.update(
                city1=origin_citycode,
                city2=destination_citycode,
                AlternativeRoute="1",
            )
            path = "/v5/direction/transit/integrated"
        else:
            raise AmapProviderError("route_mode_invalid")
        return await self._request(path, params)

    async def _request(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        now = monotonic()
        if now < self._circuit_open_until:
            raise AmapProviderError("provider_circuit_open")
        await self._acquire_rate_slot(now)
        request_params = {**params, "key": self._api_key, "output": "JSON"}
        async with self._semaphore:
            for attempt in range(self._max_retries + 1):
                try:
                    response = await self._client.get(
                        f"{self._base_url}{path}",
                        params=request_params,
                        timeout=self._timeout,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise AmapProviderError("tool_result_invalid")
                    self._validate_business_status(payload)
                except AmapProviderError as error:
                    self._record_failure()
                    raise error
                except (httpx.ConnectError, httpx.ReadTimeout) as error:
                    if attempt < self._max_retries:
                        continue
                    self._record_failure()
                    reason = (
                        "provider_timeout"
                        if isinstance(error, httpx.ReadTimeout)
                        else "provider_unavailable"
                    )
                    raise AmapProviderError(reason) from None
                except (httpx.HTTPError, ValueError):
                    self._record_failure()
                    raise AmapProviderError("provider_unavailable") from None
                self._consecutive_failures = 0
                return payload
        raise AmapProviderError("provider_unavailable")

    async def _acquire_rate_slot(self, now: float) -> None:
        async with self._rate_lock:
            while self._request_times and now - self._request_times[0] >= 60:
                self._request_times.popleft()
            if len(self._request_times) >= self._requests_per_minute:
                raise AmapProviderError("provider_rate_limited")
            self._request_times.append(now)

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= 5:
            self._circuit_open_until = monotonic() + 60

    @staticmethod
    def _validate_business_status(payload: Mapping[str, Any]) -> None:
        if str(payload.get("status")) == "1":
            return
        info_code = str(payload.get("infocode") or "")
        if info_code in {"10001", "10002", "10003", "10007", "10008", "10009"}:
            raise AmapProviderError("provider_auth_failed")
        if info_code in {"10004", "10005", "10010", "10019", "10020", "10021"}:
            raise AmapProviderError("provider_quota_exceeded")
        raise AmapProviderError("provider_unavailable")


# 国测局 GCJ-02 偏移的公开近似算法参数(WGS84 → GCJ-02 仅做本地换算,不发额外请求)。
_EARTH_A = 6378245.0
_EARTH_EE = 0.00669342162296594323


def wgs84_to_gcj02(latitude: float, longitude: float) -> tuple[float, float]:
    """把浏览器 WGS84 坐标换算为高德 GCJ-02 坐标;境外坐标原样返回。"""
    if not _inside_china(latitude, longitude):
        return latitude, longitude
    offset_lat = _transform_lat(longitude - 105.0, latitude - 35.0)
    offset_lng = _transform_lng(longitude - 105.0, latitude - 35.0)
    rad_lat = latitude / 180.0 * math.pi
    magic = 1 - _EARTH_EE * math.sin(rad_lat) * math.sin(rad_lat)
    sqrt_magic = math.sqrt(magic)
    offset_lat = (
        offset_lat * 180.0
        / ((_EARTH_A * (1 - _EARTH_EE)) / (magic * sqrt_magic) * math.pi)
    )
    offset_lng = (
        offset_lng * 180.0 / (_EARTH_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    )
    return latitude + offset_lat, longitude + offset_lng


def _inside_china(latitude: float, longitude: float) -> bool:
    return 0.8293 <= latitude <= 55.8271 and 72.004 <= longitude <= 137.8347


def _transform_lat(x: float, y: float) -> float:
    ret = (
        -100.0
        + 2.0 * x
        + 3.0 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * math.sqrt(abs(x))
    )
    ret += (
        (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi))
        * 2.0
        / 3.0
    )
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (
        160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)
    ) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = (
        300.0
        + x
        + 2.0 * y
        + 0.1 * x * x
        + 0.1 * x * y
        + 0.1 * math.sqrt(abs(x))
    )
    ret += (
        (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi))
        * 2.0
        / 3.0
    )
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (
        150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)
    ) * 2.0 / 3.0
    return ret
