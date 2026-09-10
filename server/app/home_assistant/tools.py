from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Annotated, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from app.config import HomeAssistantEntityConfig
from app.llm import ToolDefinition
from app.schemas import PrivacyLevel
from app.tools import ToolContext, ToolResult

from .directory import DeviceSearchPage
from .models import HomeAssistantError, HomeAssistantLogEntry, HomeAssistantState


class HomeStateProvider(Protocol):
    def resolve(self, target: str) -> HomeAssistantEntityConfig: ...

    def get_state(self, entity_id: str) -> HomeAssistantState: ...


class HomeControlProvider(HomeStateProvider, Protocol):
    async def control(
        self,
        target: str,
        action: str,
        *,
        service_data: dict[str, object] | None = None,
    ) -> HomeAssistantEntityConfig: ...


class HomeHistoryProvider(HomeStateProvider, Protocol):
    async def history(
        self, target: str, start: datetime, end: datetime
    ) -> tuple[HomeAssistantEntityConfig, tuple[HomeAssistantState, ...]]: ...

    async def logbook(
        self, target: str, start: datetime, end: datetime
    ) -> tuple[HomeAssistantEntityConfig, tuple[HomeAssistantLogEntry, ...]]: ...


class DeviceSearchProvider(Protocol):
    def search_devices(
        self,
        *,
        privacy_level: PrivacyLevel,
        query: str | None = None,
        room: str | None = None,
        domain: str | None = None,
        action: str | None = None,
        limit: int = 5,
        cursor: str | None = None,
    ) -> DeviceSearchPage: ...


class SearchDevicesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    room: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    domain: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")] | None = None
    action: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")] | None = None
    limit: Annotated[int, Field(ge=1, le=5)] = 5
    cursor: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class SearchDevicesTool:
    name = "search_devices"
    description = (
        "检索当前已授权的 Home Assistant 设备。设备名称不明确、按房间或类型查找时先调用；"
        "最多返回 5 项。候选只用于定位，执行控制时仍会重新检查权限。"
    )
    arguments_model: type[BaseModel] = SearchDevicesArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, provider: DeviceSearchProvider) -> None:
        self._provider = provider

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=SearchDevicesArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(SearchDevicesArgs, arguments)
        try:
            page = self._provider.search_devices(
                privacy_level=PrivacyLevel(context.privacy_level),
                query=args.query,
                room=args.room,
                domain=args.domain,
                action=args.action,
                limit=args.limit,
                cursor=args.cursor,
            )
        except HomeAssistantError as error:
            return _failure(self.name, error.reason_code, started)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider="home_assistant",
            data={
                "devices": [
                    {
                        "entity_id": item.entity_id,
                        "name": item.name,
                        "room": item.room,
                        "domain": item.domain,
                        "available": item.available,
                        "actions": list(item.actions),
                        "confirmation_required_actions": list(
                            item.confirmation_required_actions
                        ),
                        "match_kind": item.match_kind,
                    }
                    for item in page.devices
                ],
                "has_more": page.has_more,
                "next_cursor": page.next_cursor,
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )


class HomeGetStateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    targets: Annotated[list[str], Field(min_length=1, max_length=10)]


class HomeGetStateTool:
    name = "home_get_state"
    description = (
        "读取已授权 Home Assistant 实体的当前状态。targets 使用用户说出的设备名称、别名或"
        "明确 entity_id; 只用于查询, 不执行任何控制。"
    )
    arguments_model: type[BaseModel] = HomeGetStateArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, provider: HomeStateProvider) -> None:
        self._provider = provider

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=HomeGetStateArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(HomeGetStateArgs, arguments)
        results: list[dict[str, object]] = []
        try:
            for target in dict.fromkeys(args.targets):
                policy = self._provider.resolve(target)
                required = PrivacyLevel(policy.privacy_level)
                actual = PrivacyLevel(context.privacy_level)
                if required is PrivacyLevel.L3:
                    return _failure(self.name, "ha_l3_model_access_denied", started)
                if _privacy_rank(actual) < _privacy_rank(required):
                    return _failure(self.name, "ha_privacy_level_required", started)
                state = self._provider.get_state(policy.entity_id)
                attributes = {
                    key: value
                    for key, value in state.attributes.items()
                    if key in set(policy.allowed_attributes)
                }
                results.append(
                    {
                        "entity_id": policy.entity_id,
                        "name": policy.display_name,
                        "state": state.state,
                        "attributes": attributes,
                        "last_updated": (
                            state.last_updated.isoformat()
                            if state.last_updated is not None
                            else None
                        ),
                    }
                )
        except HomeAssistantError as error:
            return _failure(self.name, error.reason_code, started)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider="home_assistant",
            data={"entities": results},
            latency_ms=(perf_counter() - started) * 1_000,
        )


class HomeControlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Annotated[str, Field(min_length=1, max_length=255)]
    action: Literal[
        "turn_on",
        "turn_off",
        "toggle",
        "set_temperature",
        "set_brightness",
        "play",
        "pause",
        "volume_set",
    ]
    temperature_c: Annotated[float, Field(ge=16, le=30)] | None = None
    brightness_pct: Annotated[int, Field(ge=1, le=100)] | None = None
    volume_level: Annotated[float, Field(ge=0, le=1)] | None = None


class HomeControlTool:
    name = "home_control"
    description = (
        "控制已授权的 Home Assistant 灯、开关、空调或媒体播放器。仅可使用后台为该实体开放的动作。"
        "空调等需要确认的动作只有在用户当前消息明确包含 `确认` 或 `确定` 时才会执行。"
    )
    arguments_model: type[BaseModel] = HomeControlArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, provider: HomeControlProvider) -> None:
        self._provider = provider

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=HomeControlArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(HomeControlArgs, arguments)
        try:
            policy = self._provider.resolve(args.target)
            privacy_error = _privacy_error(policy, context)
            if privacy_error is not None:
                return _failure(self.name, privacy_error, started)
            if args.action not in policy.allowed_actions:
                return _failure(self.name, "ha_action_denied", started)
            if args.action == "set_temperature" and args.temperature_c is None:
                return _failure(self.name, "ha_temperature_required", started)
            if args.action == "set_brightness" and args.brightness_pct is None:
                return _failure(self.name, "ha_brightness_required", started)
            if args.action == "volume_set" and args.volume_level is None:
                return _failure(self.name, "ha_volume_required", started)
            if (
                args.action in policy.confirmation_required_actions
                and not _has_explicit_confirmation(context.user_text)
            ):
                return ToolResult(
                    ok=False,
                    tool_name=self.name,
                    provider="home_assistant",
                    reason_code="ha_confirmation_required",
                    data={
                        "target": policy.display_name,
                        "action": args.action,
                        "temperature_c": args.temperature_c,
                        "brightness_pct": args.brightness_pct,
                        "volume_level": args.volume_level,
                    },
                    latency_ms=(perf_counter() - started) * 1_000,
                )
            service_data: dict[str, object] | None = None
            if args.action == "set_temperature":
                service_data = {"temperature": args.temperature_c}
            elif args.action == "set_brightness":
                service_data = {"brightness_pct": args.brightness_pct}
            elif args.action == "volume_set":
                service_data = {"volume_level": args.volume_level}
            await self._provider.control(
                policy.entity_id,
                args.action,
                service_data=service_data,
            )
        except HomeAssistantError as error:
            return _failure(self.name, error.reason_code, started)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider="home_assistant",
            data={
                "entity_id": policy.entity_id,
                "name": policy.display_name,
                "action": args.action,
                "temperature_c": args.temperature_c,
                "brightness_pct": args.brightness_pct,
                "volume_level": args.volume_level,
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )


class HomeGetHistoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Annotated[str, Field(min_length=1, max_length=255)]
    hours: Annotated[int, Field(ge=1, le=168)] = 24
    limit: Annotated[int, Field(ge=1, le=100)] = 50
    include_logbook: bool = True


class HomeGetHistoryTool:
    name = "home_get_history"
    description = (
        "读取已授权 Home Assistant 实体的状态历史和 Logbook。适合回答灯何时开关、"
        "传感器何时变化、设备最近发生了什么。时间范围和返回条数受后台策略限制。"
    )
    arguments_model: type[BaseModel] = HomeGetHistoryArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, provider: HomeHistoryProvider) -> None:
        self._provider = provider

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=HomeGetHistoryArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(HomeGetHistoryArgs, arguments)
        try:
            policy = self._provider.resolve(args.target)
            privacy_error = _privacy_error(policy, context)
            if privacy_error is not None:
                return _failure(self.name, privacy_error, started)
            if not policy.history_allowed:
                return _failure(self.name, "ha_history_denied", started)
            hours = min(args.hours, policy.history_max_hours)
            end = datetime.now(UTC)
            start = end - timedelta(hours=hours)
            _, states = await self._provider.history(policy.entity_id, start, end)
            log_entries: tuple[HomeAssistantLogEntry, ...] = ()
            if args.include_logbook:
                _, log_entries = await self._provider.logbook(policy.entity_id, start, end)
        except HomeAssistantError as error:
            return _failure(self.name, error.reason_code, started)
        state_rows = [
            {
                "state": state.state,
                "changed_at": (
                    state.last_changed.isoformat() if state.last_changed is not None else None
                ),
            }
            for state in states[-args.limit :]
        ]
        log_rows = [
            {
                "when": entry.when.isoformat() if entry.when is not None else None,
                "name": entry.name,
                "message": entry.message,
                "domain": entry.domain,
            }
            for entry in log_entries[-args.limit :]
        ]
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider="home_assistant",
            data={
                "entity_id": policy.entity_id,
                "name": policy.display_name,
                "hours": hours,
                "states": state_rows,
                "logbook": log_rows,
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )


def _privacy_rank(value: PrivacyLevel) -> int:
    return {
        PrivacyLevel.L0: 0,
        PrivacyLevel.L1: 1,
        PrivacyLevel.L2: 2,
        PrivacyLevel.L3: 3,
    }[value]


def _privacy_error(
    policy: HomeAssistantEntityConfig,
    context: ToolContext,
) -> str | None:
    required = PrivacyLevel(policy.privacy_level)
    actual = PrivacyLevel(context.privacy_level)
    if required is PrivacyLevel.L3:
        return "ha_l3_model_access_denied"
    if _privacy_rank(actual) < _privacy_rank(required):
        return "ha_privacy_level_required"
    return None


def _has_explicit_confirmation(user_text: str | None) -> bool:
    if not user_text:
        return False
    normalized = user_text.strip().casefold()
    if any(
        phrase in normalized
        for phrase in (
            "不确认",
            "不确定",
            "别确认",
            "不要",
            "取消",
            "暂不",
            "先不",
        )
    ):
        return False
    return "确认" in normalized or "确定" in normalized


def _failure(tool_name: str, reason_code: str, started: float) -> ToolResult:
    data = (
        {"suggested_tool": "search_devices"}
        if reason_code in {"ha_entity_not_found", "ha_target_ambiguous"}
        else {}
    )
    return ToolResult(
        ok=False,
        tool_name=tool_name,
        provider="home_assistant",
        reason_code=reason_code,
        data=data,
        latency_ms=(perf_counter() - started) * 1_000,
    )
