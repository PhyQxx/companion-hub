"""位置解析链与连接级临时位置(docs/05 地图/天气章节 §6)。

优先级:本轮明确地址/城市 > 本连接临时位置(TTL 15 分钟)> 默认城市 > 询问用户。
精确坐标是 L2 原始信号:仅存在于内存与本轮 provider 调用参数,
不入消息/Timeline/记忆/日志,decision_meta 只记 location_source 枚举。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field

from app.schemas.common import StrictModel

from .amap import AmapProvider, AmapProviderError, wgs84_to_gcj02

LOCATION_TTL = timedelta(minutes=15)

LocationSource = Literal["explicit", "ephemeral", "default_city"]


class ClientLocation(StrictModel):
    """客户端上报的 WGS84 临时位置。received_at 由服务端打点,不信任客户端时钟。"""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(default=0, ge=0, le=100_000)
    received_at: datetime

    def is_fresh(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        received = self.received_at
        if received.tzinfo is None:
            received = received.replace(tzinfo=UTC)
        return current - received <= LOCATION_TTL


class ClientLocationPayload(StrictModel):
    """入站帧 location 字段的载荷。日志脱敏键已覆盖 location/coordinates。"""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(default=0, ge=0, le=100_000)

    def to_client_location(self) -> ClientLocation:
        return ClientLocation(
            latitude=self.latitude,
            longitude=self.longitude,
            accuracy_m=self.accuracy_m,
            received_at=datetime.now(UTC),
        )


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    """解析结果。gcj_location 仅用于本轮 provider 调用,不得序列化进持久化记录。"""

    source: LocationSource
    name: str
    adcode: str
    citycode: str | None
    gcj_location: str | None


def normalize_explicit(value: str | None) -> str | None:
    """模型可能把“当前位置”字面量当参数传入;统一视为未指定,走解析链。"""
    if value is None or value.strip() == "" or value.strip() == "current_location":
        return None
    return value.strip()


async def resolve_location(
    provider: AmapProvider,
    *,
    explicit: str | None,
    ephemeral: ClientLocation | None,
    default_city: str | None,
) -> ResolvedLocation:
    try:
        if explicit is not None:
            # 显式地点不带默认城市 hint,避免“北京天气”被误解析回默认城市。
            return _from_geocode(
                await provider.geocode(explicit, city=None), "explicit", explicit
            )
        if ephemeral is not None and ephemeral.is_fresh():
            return _from_regeo(
                await provider.regeo(_gcj_location(ephemeral)), ephemeral
            )
        if default_city:
            return _from_geocode(
                await provider.geocode(default_city, city=default_city),
                "default_city",
                default_city,
            )
    except AmapProviderError:
        raise
    raise AmapProviderError("location_required")


def _gcj_location(ephemeral: ClientLocation) -> str:
    latitude, longitude = wgs84_to_gcj02(ephemeral.latitude, ephemeral.longitude)
    return f"{longitude:.6f},{latitude:.6f}"


def _from_geocode(
    item: Mapping[str, object], source: LocationSource, fallback_name: str
) -> ResolvedLocation:
    return ResolvedLocation(
        source=source,
        name=str(item.get("formatted_address") or fallback_name),
        adcode=str(item.get("adcode") or ""),
        citycode=str(item.get("citycode") or "") or None,
        gcj_location=str(item.get("location") or "") or None,
    )


def _from_regeo(
    regeocode: Mapping[str, object], ephemeral: ClientLocation
) -> ResolvedLocation:
    component = regeocode.get("addressComponent")
    if not isinstance(component, Mapping) or not component.get("adcode"):
        raise AmapProviderError("tool_result_invalid")
    # 临时位置只回注到城市/区县精度,不把街道级 formatted_address 带进模型上下文。
    return ResolvedLocation(
        source="ephemeral",
        name=_component_name(component),
        adcode=str(component.get("adcode") or ""),
        citycode=str(component.get("citycode") or "") or None,
        gcj_location=_gcj_location(ephemeral),
    )


def _component_name(component: Mapping[str, object]) -> str:
    parts = [
        str(component.get(key))
        for key in ("province", "city", "district")
        if isinstance(component.get(key), str) and component.get(key)
    ]
    return "".join(parts) or "当前位置"
