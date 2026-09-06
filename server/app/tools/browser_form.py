"""WEB-01 浏览器工作流工具：打开页面、读取表单、填写、提交。

红线（docs/39 J5「提交前显示目标、字段和证据」）：填写与提交分离——
`browser_form_fill` 只写值永不触发提交；`browser_form_submit` 在
Action Registry 中为 A2 每次确认，只能经计划—确认—执行链路触发。
四个工具都不挂载为聊天工具（`_device_tool_ready` 恒 False），由
`tools.browser_workflow.enabled` 总开关把关，默认关闭。

ref 契约：扩展端按文档顺序枚举可见表单控件，`f{序号}`/`form{序号}`；
页面结构变化会导致 control_not_found 而不是静默错位。密码框在扩展端
硬跳过，值也不回传。
"""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Annotated, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.config import BrowserWorkflowConfig
from app.llm import ToolDefinition
from app.schemas import PrivacyLevel

from .contracts import ToolContext, ToolResult
from .desktop import DesktopCommand, DesktopCommandGateway, DesktopTargetResolver

ConfigProvider = Callable[[], BrowserWorkflowConfig]

FIELD_REF_PATTERN = r"^f\d+$"
FORM_REF_PATTERN = r"^form\d+$"


class BrowserTargetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Annotated[str, Field(min_length=1, max_length=160)] | None = None


class BrowserOpenTabArgs(BrowserTargetArgs):
    url: Annotated[str, Field(min_length=1, max_length=2048)]


class BrowserFormReadArgs(BrowserTargetArgs):
    pass


class FormFieldRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: Annotated[str, Field(pattern=FIELD_REF_PATTERN)]
    value: Annotated[str, Field(min_length=1, max_length=5000)]


class BrowserFormFillArgs(BrowserTargetArgs):
    fields: Annotated[list[FormFieldRef], Field(min_length=1, max_length=50)]


class BrowserFormSubmitArgs(BrowserTargetArgs):
    form_ref: Annotated[str, Field(pattern=FORM_REF_PATTERN)]


class _BrowserWorkflowTool:
    """四个命令共用的下发骨架：总开关 → 设备解析 → 命令终态。"""

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
        started: float,
        ttl_seconds: int = 10,
    ) -> ToolResult:
        if context.user_id is None:
            return _failure(tool_name, "action_user_required", started)
        if context.idempotency_key is None:
            return _failure(tool_name, "action_idempotency_required", started)
        if not self._config_provider().enabled:
            return _failure(tool_name, "browser_workflow_not_allowed", started)
        try:
            device = await self._resolver.resolve(
                owner_user_id=context.user_id,
                target=target or None,
                capability=capability,
            )
        except RuntimeError:
            return _failure(tool_name, "browser_channel_unavailable", started)
        issued = await self._gateway.issue(
            device_id=device.id,
            command=command,
            args=args,
            idempotency_key=context.idempotency_key,
            ttl_seconds=ttl_seconds,
        )
        terminal: DesktopCommand = await self._gateway.wait_for_terminal(
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


class BrowserOpenTabTool:
    name = "browser_open_tab"
    description = (
        "在用户已授权的浏览器设备上新开一个标签页并导航到指定 URL。只允许"
        " http/https；需要 tools.browser_workflow.enabled 总开关。"
    )
    arguments_model: type[BaseModel] = BrowserOpenTabArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L1

    def __init__(
        self,
        resolver: DesktopTargetResolver,
        gateway: DesktopCommandGateway,
        config_provider: ConfigProvider,
    ) -> None:
        self._base = _BrowserWorkflowTool(resolver, gateway, config_provider)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=BrowserOpenTabArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(BrowserOpenTabArgs, arguments)
        url = args.url.strip()
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not (parts.hostname or ""):
            return _failure(self.name, "invalid_target_url", started)
        return await self._base.dispatch(
            tool_name=self.name,
            capability="browser.tab.open",
            command="browser.tab.open",
            args={"url": url},
            target=args.target,
            context=context,
            started=started,
        )


class BrowserFormReadTool:
    name = "browser_form_read"
    description = (
        "读取浏览器设备当前页面的可见表单控件（顺序 ref、标签、占位符、"
        "是否必填；密码框值永不回传）。纯读操作，用于定位要填写的字段。"
    )
    arguments_model: type[BaseModel] = BrowserFormReadArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L1

    def __init__(
        self,
        resolver: DesktopTargetResolver,
        gateway: DesktopCommandGateway,
        config_provider: ConfigProvider,
    ) -> None:
        self._base = _BrowserWorkflowTool(resolver, gateway, config_provider)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=BrowserFormReadArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(BrowserFormReadArgs, arguments)
        return await self._base.dispatch(
            tool_name=self.name,
            capability="browser.form.read",
            command="browser.form.read",
            args={},
            target=args.target,
            context=context,
            started=started,
        )


class BrowserFormFillTool:
    name = "browser_form_fill"
    description = (
        "向浏览器页面表单填写字段值（ref 来自 browser_form_read），只写值"
        "绝不提交。密码框会被跳过。用户必须先看到并认可要写入的每个字段值。"
    )
    arguments_model: type[BaseModel] = BrowserFormFillArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L1

    def __init__(
        self,
        resolver: DesktopTargetResolver,
        gateway: DesktopCommandGateway,
        config_provider: ConfigProvider,
    ) -> None:
        self._base = _BrowserWorkflowTool(resolver, gateway, config_provider)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=BrowserFormFillArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(BrowserFormFillArgs, arguments)
        return await self._base.dispatch(
            tool_name=self.name,
            capability="browser.form.fill",
            command="browser.form.fill",
            args={"fields": [item.model_dump(mode="json") for item in args.fields]},
            target=args.target,
            context=context,
            started=started,
        )


class BrowserFormSubmitTool:
    name = "browser_form_submit"
    description = (
        "提交浏览器页面上的表单（form_ref 来自 browser_form_read）。这是"
        "对外发送动作：必须在计划确认中向用户展示目标站点、全部字段值和"
        "填写后截图证据，用户明确同意后才可执行。"
    )
    arguments_model: type[BaseModel] = BrowserFormSubmitArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L1

    def __init__(
        self,
        resolver: DesktopTargetResolver,
        gateway: DesktopCommandGateway,
        config_provider: ConfigProvider,
    ) -> None:
        self._base = _BrowserWorkflowTool(resolver, gateway, config_provider)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=BrowserFormSubmitArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(BrowserFormSubmitArgs, arguments)
        return await self._base.dispatch(
            tool_name=self.name,
            capability="browser.form.submit",
            command="browser.form.submit",
            args={"form_ref": args.form_ref},
            target=args.target,
            context=context,
            started=started,
        )


def _failure(tool_name: str, reason_code: str, started: float) -> ToolResult:
    return ToolResult(
        ok=False,
        tool_name=tool_name,
        reason_code=reason_code,
        latency_ms=(perf_counter() - started) * 1_000,
    )


__all__ = [
    "BrowserFormFillTool",
    "BrowserFormReadTool",
    "BrowserFormSubmitTool",
    "BrowserOpenTabTool",
]
