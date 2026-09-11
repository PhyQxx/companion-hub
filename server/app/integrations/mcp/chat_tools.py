"""MCP-C2：按意图检索并每轮动态挂载只读 MCP 工具。

红线（docs/43 §4/§5）：
- 普通对话不注入 MCP 目录，只有用户文本与工具词法相关时才挂载，
  每轮最多 `config.mcp.max_tools_per_turn` 个——工具总数增长不推高常驻 Token；
- 只挂只读工具；L2/L3 回合一律不挂（外部服务不接收私密内容）；
- 远端描述只用于本地相关性打分，绝不进入提示词；给模型的工具描述是
  Hub 生成的稳定文案，参数 Schema 经 modeling.py 保守重建（无远端自由文本）。
"""

from __future__ import annotations

import re
from time import perf_counter

from pydantic import BaseModel

from app.config import HubConfig
from app.integrations.mcp.manager import McpManager
from app.llm import ToolDefinition
from app.schemas import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult

from .modeling import build_flat_arguments_model
from .models import McpToolDescriptor

# 相关性阈值：查询 token 与工具名/标题的重叠占比，低于则视为无关
RELEVANCE_THRESHOLD = 0.12
_MAX_DESCRIPTION_CHARS = 500
_TOKEN = re.compile(r"[a-z0-9_+-]{2,}")


class McpReadToolHandler:
    name: str
    description = ""
    runs_local = False
    max_privacy_level = PrivacyLevel.L1

    def __init__(
        self,
        descriptor: McpToolDescriptor,
        arguments_model: type[BaseModel],
        manager: McpManager,
    ) -> None:
        self.name = descriptor.internal_name
        self._descriptor = descriptor
        self._manager = manager
        self.arguments_model = arguments_model

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=_stable_description(self._descriptor, self.arguments_model),
            parameters=self.arguments_model.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        del context
        started = perf_counter()
        try:
            result = await self._manager.call(
                self._descriptor.internal_name, arguments.model_dump()
            )
        except Exception as error:
            reason = getattr(error, "reason_code", "mcp_connection_failed")
            return ToolResult(
                ok=False,
                tool_name=self.name,
                provider="mcp",
                latency_ms=(perf_counter() - started) * 1_000,
                reason_code=reason,
            )
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
                "text": result.text[:2_000],
            },
        )


class McpChatToolProvider:
    """每轮按词法相关性挑选只读 MCP 工具；不相关或受限时返回空。"""

    def __init__(self, manager: McpManager) -> None:
        self._manager = manager
        self._cache: dict[str, type[BaseModel] | None] = {}

    def select(
        self,
        text: str,
        *,
        config: HubConfig,
        privacy_level: PrivacyLevel | str,
    ) -> tuple[McpReadToolHandler, ...]:
        if not config.mcp.enabled:
            return ()
        if PrivacyLevel(str(privacy_level)) not in {PrivacyLevel.L0, PrivacyLevel.L1}:
            return ()
        query_tokens = _tokens(text)
        if not query_tokens:
            return ()
        scored: list[tuple[float, McpToolDescriptor]] = []
        for descriptor in self._manager.catalog():
            if not descriptor.read_only:
                continue
            score = _relevance(query_tokens, descriptor)
            if score >= RELEVANCE_THRESHOLD:
                scored.append((score, descriptor))
        scored.sort(key=lambda item: (-item[0], item[1].internal_name))
        handlers: list[McpReadToolHandler] = []
        for _, descriptor in scored[: config.mcp.max_tools_per_turn]:
            model = self._model_for(descriptor)
            if model is None:
                continue
            handlers.append(McpReadToolHandler(descriptor, model, self._manager))
        return tuple(handlers)

    def _model_for(self, descriptor: McpToolDescriptor) -> type[BaseModel] | None:
        if descriptor.internal_name in self._cache:
            return self._cache[descriptor.internal_name]
        model = build_flat_arguments_model(descriptor.input_schema)
        self._cache[descriptor.internal_name] = model
        return model


def _stable_description(
    descriptor: McpToolDescriptor, model: type[BaseModel]
) -> str:
    """给模型的稳定描述：不含远端自由文本，只暴露功能定位与参数名。"""
    fields = ", ".join(sorted(model.model_json_schema().get("properties", {})))
    text = (
        f"外部只读工具（MCP {descriptor.server_id}）：{descriptor.remote_name}。"
        f"参数：{fields or '无'}。返回外部服务的裁剪结果。"
    )
    return text[:_MAX_DESCRIPTION_CHARS]


def _relevance(query_tokens: set[str], descriptor: McpToolDescriptor) -> float:
    """词法相关性：查询 token 与工具名分段/标题的重叠率（远端描述仅辅助）。"""
    target = set()
    for segment in descriptor.internal_name.replace(".", " ").replace("-", " ").split():
        target.add(segment.casefold())
        target.update(_tokens(segment))
    for token in _tokens(descriptor.title):
        target.add(token)
    if not target:
        return 0.0
    overlap = len(query_tokens & target)
    if overlap == 0:
        return 0.0
    return overlap / len(query_tokens)


def _tokens(text: str) -> set[str]:
    lowered = text.casefold()
    tokens = set(_TOKEN.findall(lowered))
    # 中文按 2-gram 切分，避免整句成一个 token
    compact = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", lowered)
    tokens.update(
        compact[index : index + 2]
        for index in range(max(len(compact) - 1, 0))
        if re.match(r"[\u4e00-\u9fff]{2}", compact[index : index + 2])
    )
    return {token for token in tokens if len(token) >= 2}
