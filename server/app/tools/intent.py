from __future__ import annotations

from collections.abc import Callable, Iterable

from app.config import HubConfig

_WEATHER_TERMS = ("天气", "气温", "温度", "下雨", "降雨", "晴天", "阴天", "台风")
_NEARBY_TERMS = ("附近", "最近", "周边", "医院", "药店", "餐厅", "充电站")
_ROUTE_TERMS = ("怎么走", "路线", "导航", "开车去", "步行去", "坐公交")
_SCREEN_TERMS = (
    "看一下电脑",
    "看下电脑",
    "看看电脑",
    "看一下屏幕",
    "看下屏幕",
    "看看屏幕",
    "我的电脑",
    "电脑屏幕",
    "当前屏幕",
    "屏幕上",
    "屏幕内容",
    "当前桌面",
    "桌面上",
    "圈选",
    "框选",
    "截图",
    "截屏",
    "选择器",
    "分享屏幕",
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
_HOME_STATE_TERMS = (
    "开着吗",
    "开着",
    "关着吗",
    "关着",
    "亮着",
    "熄灭",
    "状态",
    "多少度",
    "温度",
    "湿度",
    "空气质量",
    "有人吗",
    "在家吗",
    "门开",
    "门关",
    "灯亮",
    "灯开",
)
_HOME_CONTROL_TERMS = (
    "打开",
    "关闭",
    "开灯",
    "关灯",
    "调到",
    "设置到",
    "设为",
    "切换",
    "确认",
    "确定",
)
_HOME_HISTORY_TERMS = (
    "设备日志",
    "日志",
    "历史记录",
    "状态历史",
    "开关记录",
    "什么时候开",
    "什么时候关",
    "最近变化",
    "过去几小时",
)


def select_query_tools(text: str, config: HubConfig) -> tuple[str, ...]:
    if not config.tools.enabled or not config.tools.query.enabled:
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
    for name in DEVICE_TOOL_REQUIREMENTS:
        if supports_device_capability(name, values) and _term_hit(text, name):
            return (name,)
    return ()


def _term_hit(text: str, tool_name: str) -> bool:
    terms = _DEVICE_TOOL_TERMS.get(tool_name, ())
    return any(term in text for term in terms)


def supports_device_capability(tool_name: str, capability_ids: Iterable[str]) -> bool:
    """设备工具的能力要求：任一在线能力满足即视为可挂载（文本无关，由模型自选）。"""
    supports = DEVICE_TOOL_REQUIREMENTS.get(tool_name)
    if supports is None:
        return True
    return any(supports(value) for value in capability_ids)


# 工具 → 在线能力要求。挂载只看能力与配置就绪；何时调用由模型根据工具描述自行判断。
# 顺序即 select_device_tools 的确定性优先级：浏览器读取先于屏幕截图。
DEVICE_TOOL_REQUIREMENTS: dict[str, Callable[[str], bool]] = {
    "inspect_webpage": lambda value: value.endswith(":browser.current_tab.read")
    or value.endswith(":browser.current_tab.capture"),
    "capture_screen": lambda value: value.endswith(":screen.capture"),
    "home_get_history": lambda value: value.startswith("home_assistant:")
    and value.endswith(":history.read"),
    "home_control": lambda value: value.startswith("home_assistant:")
    and value.rsplit(":", 1)[-1]
    in {
        "turn_on",
        "turn_off",
        "toggle",
        "set_temperature",
        "set_brightness",
        "play",
        "pause",
        "volume_set",
    },
    "home_get_state": lambda value: value.startswith("home_assistant:")
    and value.endswith(":state.read"),
}

# 仅用于确定性 HA 读回退（_deterministic_home_read_call）与选择器单测的关键词表。
_DEVICE_TOOL_TERMS: dict[str, tuple[str, ...]] = {
    "inspect_webpage": _WEBPAGE_TERMS,
    "capture_screen": _SCREEN_TERMS,
    "home_get_history": _HOME_HISTORY_TERMS,
    "home_control": _HOME_CONTROL_TERMS,
    "home_get_state": _HOME_STATE_TERMS,
}
