"""聊天域共享视图模型与回合载体。

从 service.py 抽出，供回合装配/提交/维护处理器等协作者模块共同引用，
避免协作者反向依赖 service 形成运行时循环导入。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.cognition import CognitiveDecision
from app.config import HubConfig
from app.harness.context import ContextReference
from app.integrations.mcp.chat_tools import McpReadToolHandler
from app.llm import CompletionRequest, CompletionResult
from app.memory import RetrievalResult
from app.persona import PersonaConfig
from app.skills.runtime import SkillReadToolHandler
from app.timeline import (
    BrowserActivityRecallResult,
    HistoryRecallResult,
    ScreenActivityRecallResult,
)
from app.tools import ClientLocation

from .capabilities import RuntimeActionCapability


class CompletionBackend(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...

    async def stream(
        self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
    ) -> CompletionResult: ...


@dataclass(frozen=True, slots=True)
class ConversationView:
    id: UUID
    user_id: UUID
    title: str | None
    status: str
    last_seq: int
    created_at: datetime
    last_active_at: datetime


@dataclass(frozen=True, slots=True)
class MessageView:
    id: UUID
    conversation_id: UUID
    turn_id: UUID
    seq: int
    role: str
    content: str
    privacy_level: str
    generation_id: UUID | None
    decision_meta: dict[str, object] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChatTurn:
    user_message: MessageView
    assistant_message: MessageView


@dataclass(frozen=True, slots=True)
class PendingTurn:
    turn_id: UUID
    generation_id: UUID
    conversation_id: UUID
    user_id: UUID
    turn_seq: int
    user_message: MessageView
    request: CompletionRequest
    config: HubConfig
    config_version: int
    persona: PersonaConfig
    persona_version: int
    user_timezone: str
    memory_retrieval: RetrievalResult | None = None
    history_recall: HistoryRecallResult | None = None
    screen_activity_recall: ScreenActivityRecallResult | None = None
    browser_activity_recall: BrowserActivityRecallResult | None = None
    runtime_capabilities: tuple[RuntimeActionCapability, ...] = ()
    tool_names: tuple[str, ...] = ()
    mcp_handlers: tuple[McpReadToolHandler, ...] = ()
    skill_handlers: tuple[SkillReadToolHandler, ...] = ()
    # 连接级临时位置(L2 原始信号): 仅随轮次存活于内存, 不写入任何持久化记录。
    client_location: ClientLocation | None = None
    cognitive_decision: CognitiveDecision | None = None
    # CTX：会话滚动摘要快照（摘要文本, 覆盖到的 seq 水位），随 start_turn 事务读取
    conversation_summary: tuple[str, int] | None = None
    context_manifest: tuple[dict[str, object], ...] = ()
    context_references: tuple[ContextReference, ...] = ()
    parent_run_id: UUID | None = None
    parent_budget_enabled: bool = False
    parent_budget_scope: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class RecentDeviceReference:
    entity_id: str
    name: str
    user_id: UUID
    source_turn_seq: int
    updated_at: datetime


class TurnCancelled(RuntimeError):
    pass
