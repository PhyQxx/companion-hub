from __future__ import annotations

from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from app.llm import ToolDefinition
from app.schemas import PrivacyLevel
from app.schemas.common import StrictModel, TokenName

from .location import ClientLocation


class ToolContext(StrictModel):
    privacy_level: PrivacyLevel
    user_id: UUID | None = None
    turn_id: UUID | None = None
    # 原始当前用户消息, 仅供服务端执行策略判断; 工具不得把它写入结果或日志。
    user_text: str | None = None
    default_city: str | None = None
    # 持久化动作步骤提供的幂等键；有副作用的工具只能复用，不能自行生成替代键。
    idempotency_key: str | None = None
    # 连接级临时位置(TTL 15 分钟):仅内存传递,禁止写入日志或持久化记录。
    ephemeral_location: ClientLocation | None = None


class ToolResult(StrictModel):
    ok: bool
    tool_name: TokenName
    data: dict[str, Any] = Field(default_factory=dict)
    reason_code: TokenName | None = None
    provider: TokenName | None = None
    latency_ms: float = Field(ge=0)
    cache_hit: bool = False
    # 位置解析来源摘要(docs/35 §6.2):只记枚举,不记坐标或原始地址。
    location_source: Literal["explicit", "ephemeral", "default_city"] | None = None


class ToolExecution(StrictModel):
    call_id: str
    result: ToolResult


class ToolHandler(Protocol):
    name: str
    description: str
    arguments_model: type[BaseModel]

    def definition(self) -> ToolDefinition: ...

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult: ...
