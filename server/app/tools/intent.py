from __future__ import annotations

from collections.abc import Iterable

from app.config import HubConfig

_WEATHER_TERMS = ("天气", "气温", "温度", "下雨", "降雨", "晴天", "阴天", "台风")
_NEARBY_TERMS = ("附近", "最近", "周边", "医院", "药店", "餐厅", "充电站")
_ROUTE_TERMS = ("怎么走", "路线", "导航", "开车去", "步行去", "坐公交")
_SCREEN_TERMS = (
    "看一下电脑",
    "看下电脑",
    "看看电脑",
    "我的电脑",
    "电脑屏幕",
    "当前屏幕",
    "桌面上",
    "屏幕上",
)
_WEBPAGE_TERMS = (
    "电脑网页",
    "当前网页",
    "这个网页",
    "当前页面",
    "这个页面",
    "浏览器",
    "标签页",
    "网站上",
    "网页上",
)


def select_query_tools(text: str, config: HubConfig) -> tuple[str, ...]:
    if not config.tools.enabled:
        return ()
    selected: list[str] = []
    query = config.tools.query
    if query.weather_enabled and any(term in text for term in _WEATHER_TERMS):
        selected.append("get_weather")
    if query.nearby_enabled and any(term in text for term in _NEARBY_TERMS):
        selected.append("search_nearby")
    if query.route_enabled and any(term in text for term in _ROUTE_TERMS):
        selected.append("plan_route")
    return tuple(selected)


def select_device_tools(text: str, capability_ids: Iterable[str]) -> tuple[str, ...]:
    values = tuple(capability_ids)
    has_browser = any(
        value.endswith(":browser.current_tab.read")
        or value.endswith(":browser.current_tab.capture")
        for value in values
    )
    if has_browser and any(term in text for term in _WEBPAGE_TERMS):
        return ("inspect_webpage",)
    has_screen = any(value.endswith(":screen.capture") for value in values)
    if has_screen and any(term in text for term in _SCREEN_TERMS):
        return ("capture_screen",)
    return ()
