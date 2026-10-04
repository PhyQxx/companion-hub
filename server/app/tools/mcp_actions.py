"""MCP-D 执行工具：仅供确认后的行动计划调用外部 MCP 写工具。

不挂载为聊天工具（chat/service._device_tool_ready 恒 False），唯一入口是
Action Registry 编译出的计划步骤；runs_local=False + max L1 使 EgressGuard
在执行层兜底拦截 L2 内容外发。
"""

from __future__ import annotations

import json
from time import perf_counter
from typing import Annotated, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.integrations.mcp import McpManager, McpManagerError
from app.llm import ToolDefinition
from app.schemas import PrivacyLevel

from .contracts import ToolContext, ToolResult

MAX_MCP_ARGUMENT_BYTES = 16_000
MAX_MCP_TEXT_CHARS = 2_000
MCP_TOOL_NAME_PATTERN = r"^mcp\.[a-z0-9_-]{1,63}\.[A-Za-z0-9_-]{1,128}$"


class McpToolCallArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: Annotated[str, Field(pattern=MCP_TOOL_NAME_PATTERN)]
    catalogue_ticket: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict, max_length=16)


class McpToolCallTool:
    name = "mcp_tool_call"
    description = (
        "调用白名单 MCP 外部工具（写操作）。仅可经用户确认的行动计划触发，"
        "不接受聊天直接调用；参数与结果体积均有界。"
    )
    arguments_model: type[BaseModel] = McpToolCallArgs
    runs_local = False
    max_privacy_level = PrivacyLevel.L1

    def __init__(self, manager: McpManager) -> None:
        self._manager = manager

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=McpToolCallArgs.model_json_schema(),
        )

    async def check_admission(self, arguments: BaseModel, context: ToolContext) -> str | None:
        args = cast(McpToolCallArgs, arguments)
        if (
            len(json.dumps(args.arguments, ensure_ascii=False, default=str).encode())
            > MAX_MCP_ARGUMENT_BYTES
        ):
            return "mcp_arguments_too_large"
        try:
            self._manager.validate_catalogue_ticket(args.tool, args.catalogue_ticket, write=True)
        except McpManagerError as error:
            return error.reason_code
        return None

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        del context
        args = cast(McpToolCallArgs, arguments)
        started = perf_counter()
        encoded = json.dumps(args.arguments, ensure_ascii=False, default=str).encode()
        if len(encoded) > MAX_MCP_ARGUMENT_BYTES:
            return self._failure("mcp_arguments_too_large", started)
        if args.catalogue_ticket is None:
            return ToolResult(
                admission_status="not_admitted",
                ok=False,
                tool_name=self.name,
                provider="mcp",
                latency_ms=(perf_counter() - started) * 1_000,
                reason_code="mcp_tool_source_missing",
            )
        try:
            result = await self._manager.call_write(
                args.tool, args.arguments, catalogue_ticket=args.catalogue_ticket
            )
        except McpManagerError as error:
            return self._failure(error.reason_code, started, not_admitted=True)
        return ToolResult(
            ok=result.ok,
            tool_name=self.name,
            provider="mcp",
            latency_ms=(perf_counter() - started) * 1_000,
            reason_code=result.reason_code,
            data={
                "server_id": result.server_id,
                "tool_name": result.tool_name,
                "ok": result.ok,
                "data": result.data,
                "text": result.text[:MAX_MCP_TEXT_CHARS],
            },
        )

    def _failure(
        self, reason_code: str, started: float, *, not_admitted: bool = False
    ) -> ToolResult:
        return ToolResult(
            admission_status="not_admitted" if not_admitted else "unknown",
            ok=False,
            tool_name=self.name,
            provider="mcp",
            latency_ms=(perf_counter() - started) * 1_000,
            reason_code=reason_code,
        )
