"""PC-01 桌面白名单动作工具：打开应用/URL、设置音量、写剪贴板。

红线（docs/39 J5）：模型不得获得无限制的桌面权限——所有动作都是声明式
参数 + Hub 配置白名单硬校验，v1 不挂载为聊天工具，只能经 Action
Registry 的计划—确认—执行链路触发。设备端对每个命令再做结构校验
（scheme、音量范围、剪贴板长度）与锁屏拒绝，双层把关。
"""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Annotated, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.config import DesktopActionsConfig
from app.llm import ToolDefinition
from app.schemas import PrivacyLevel

from .contracts import ToolContext, ToolResult
from .desktop import DesktopCommandGateway, DesktopTargetResolver

CAPABILITY_OPEN_APP = "desktop.open_app"
CAPABILITY_OPEN_URL = "desktop.open_url"
CAPABILITY_VOLUME = "system.volume"
CAPABILITY_CLIPBOARD = "clipboard.write"

ConfigProvider = Callable[[], DesktopActionsConfig]


class DesktopActionArgs(BaseModel):
    """共用参数：可选目标设备（别名/UUID），能力要求由各命令决定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Annotated[str, Field(min_length=1, max_length=160)] | None = None


class DesktopOpenAppArgs(DesktopActionArgs):
    app: Annotated[str, Field(min_length=1, max_length=80)]


class DesktopOpenUrlArgs(DesktopActionArgs):
    url: Annotated[str, Field(min_length=1, max_length=2048)]


class DesktopSetVolumeArgs(DesktopActionArgs):
    volume: Annotated[int, Field(ge=0, le=100)]


class DesktopClipboardWriteArgs(DesktopActionArgs):
    text: Annotated[str, Field(min_length=1, max_length=5000)]


class _DesktopActionTool:
    """四个动作共用的下发骨架：白名单/结构校验 → 设备解析 → 命令终态。"""

    def __init__(
        self,
        resolver: DesktopTargetResolver,
        gateway: DesktopCommandGateway,
        config_provider: ConfigProvider,
    ) -> None:
        self._resolver = resolver
        self._gateway = gateway
        self._config_provider = config_provider

    async def dispatch(
        self,
        *,
        tool_name: str,
        capability: str,
        command: str,
        args: dict[str, JsonValue],
        target: str | None,
        context: ToolContext,
        gate: Callable[[DesktopActionsConfig], bool],
        started: float,
        ttl_seconds: int = 8,
    ) -> ToolResult:
        if context.user_id is None:
            return _failure(tool_name, "action_user_required", started)
        if context.idempotency_key is None:
            return _failure(tool_name, "action_idempotency_required", started)
        if not gate(self._config_provider()):
            return _failure(tool_name, "desktop_action_not_allowed", started)
        try:
            device = await self._resolver.resolve(
                owner_user_id=context.user_id,
                target=target or None,
                capability=capability,
            )
        except RuntimeError:
            return _failure(tool_name, "desktop_channel_unavailable", started)
        issued = await self._gateway.issue(
            device_id=device.id,
            command=command,
            args=args,
            idempotency_key=context.idempotency_key,
            ttl_seconds=ttl_seconds,
        )
        terminal = await self._gateway.wait_for_terminal(
            issued.id, timeout_seconds=ttl_seconds + 1
        )
        ok = terminal.status == "succeeded"
        data: dict[str, JsonValue] = {
            "command_id": str(issued.id),
            "device_id": str(device.id),
            "status": terminal.status,
        }
        return ToolResult(
            ok=ok,
            tool_name=tool_name,
            provider="device_command",
            data=data,
            reason_code=None if ok else terminal.reason_code or "device_command_failed",
            latency_ms=(perf_counter() - started) * 1_000,
        )


class DesktopOpenAppTool:
    name = "desktop_open_app"
    description = (
        "打开白名单中的桌面应用。只有配置 tools.desktop_actions.allowed_apps 里"
        "显式列出的应用名可用；不在白名单会被拒绝。"
    )
    arguments_model: type[BaseModel] = DesktopOpenAppArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L1

    def __init__(
        self,
        resolver: DesktopTargetResolver,
        gateway: DesktopCommandGateway,
        config_provider: ConfigProvider,
    ) -> None:
        self._base = _DesktopActionTool(resolver, gateway, config_provider)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=DesktopOpenAppArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(DesktopOpenAppArgs, arguments)
        app = args.app.strip()
        return await self._base.dispatch(
            tool_name=self.name,
            capability=CAPABILITY_OPEN_APP,
            command="desktop.open_app",
            args={"app": app},
            target=args.target,
            context=context,
            gate=lambda config: config.enabled and app.lower() in config.allowed_apps,
            started=started,
        )


class DesktopOpenUrlTool:
    name = "desktop_open_url"
    description = (
        "在用户桌面设备的默认浏览器打开一个 URL。scheme 必须在白名单"
        "（默认 http/https），主机名在配置了 allowed_url_hosts 时必须精确匹配。"
    )
    arguments_model: type[BaseModel] = DesktopOpenUrlArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L1

    def __init__(
        self,
        resolver: DesktopTargetResolver,
        gateway: DesktopCommandGateway,
        config_provider: ConfigProvider,
    ) -> None:
        self._base = _DesktopActionTool(resolver, gateway, config_provider)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=DesktopOpenUrlArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(DesktopOpenUrlArgs, arguments)
        url = args.url.strip()
        scheme, host = split_url(url)
        return await self._base.dispatch(
            tool_name=self.name,
            capability=CAPABILITY_OPEN_URL,
            command="desktop.open_url",
            args={"url": url},
            target=args.target,
            context=context,
            gate=lambda config: (
                config.enabled
                and scheme in config.allowed_url_schemes
                and host != ""
                and (not config.allowed_url_hosts or host in config.allowed_url_hosts)
            ),
            started=started,
        )


class DesktopSetVolumeTool:
    name = "desktop_set_volume"
    description = "设置用户桌面设备的系统输出音量（0-100）。需要配置开关 allow_volume。"
    arguments_model: type[BaseModel] = DesktopSetVolumeArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L1

    def __init__(
        self,
        resolver: DesktopTargetResolver,
        gateway: DesktopCommandGateway,
        config_provider: ConfigProvider,
    ) -> None:
        self._base = _DesktopActionTool(resolver, gateway, config_provider)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=DesktopSetVolumeArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(DesktopSetVolumeArgs, arguments)
        return await self._base.dispatch(
            tool_name=self.name,
            capability=CAPABILITY_VOLUME,
            command="system.volume.set",
            args={"volume": args.volume},
            target=args.target,
            context=context,
            gate=lambda config: config.enabled and config.allow_volume,
            started=started,
        )


class DesktopClipboardWriteTool:
    name = "desktop_clipboard_write"
    description = (
        "把一段文本写入用户桌面设备的剪贴板（≤5000 字符）。需要配置开关"
        " allow_clipboard；Action Registry 中列为 A2 每次确认。"
    )
    arguments_model: type[BaseModel] = DesktopClipboardWriteArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L1

    def __init__(
        self,
        resolver: DesktopTargetResolver,
        gateway: DesktopCommandGateway,
        config_provider: ConfigProvider,
    ) -> None:
        self._base = _DesktopActionTool(resolver, gateway, config_provider)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=DesktopClipboardWriteArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(DesktopClipboardWriteArgs, arguments)
        return await self._base.dispatch(
            tool_name=self.name,
            capability=CAPABILITY_CLIPBOARD,
            command="clipboard.write",
            args={"text": args.text},
            target=args.target,
            context=context,
            gate=lambda config: config.enabled and config.allow_clipboard,
            started=started,
        )


def split_url(url: str) -> tuple[str, str]:
    """拆出 (scheme, host) 并统一小写；非法 URL 返回空串。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "", ""
    return parts.scheme.lower(), (parts.hostname or "").lower()


def _failure(tool_name: str, reason_code: str, started: float) -> ToolResult:
    return ToolResult(
        ok=False,
        tool_name=tool_name,
        reason_code=reason_code,
        latency_ms=(perf_counter() - started) * 1_000,
    )


__all__ = [
    "CAPABILITY_CLIPBOARD",
    "CAPABILITY_OPEN_APP",
    "CAPABILITY_OPEN_URL",
    "CAPABILITY_VOLUME",
    "DesktopClipboardWriteTool",
    "DesktopOpenAppTool",
    "DesktopOpenUrlTool",
    "DesktopSetVolumeTool",
    "split_url",
]
