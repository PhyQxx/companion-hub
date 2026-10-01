from __future__ import annotations

from collections.abc import Iterable

from app.chat import ChatService, RuntimeCapabilityProvider
from app.config import DatabaseConfigStore
from app.db import Database
from app.integrations.mcp.chat_tools import McpChatToolProvider
from app.tools import ToolHandler

from ..domain import DomainAssembly


def build_conversation(
    runtime_database: Database,
    runtime_config: DatabaseConfigStore,
    *,
    domain: DomainAssembly,
    capability_provider: RuntimeCapabilityProvider | None,
    device_tools: Iterable[ToolHandler],
) -> ChatService:
    return ChatService(
        runtime_database,
        runtime_config,
        persona_store=domain.persona_store,
        memory_store=domain.memory_store,
        memory_extractor=domain.memory_extractor,
        timeline_store=domain.timeline_store,
        history_recall_service=domain.history_recall,
        capability_provider=capability_provider,
        device_tools=device_tools,
        mcp_tools=(
            McpChatToolProvider(domain.mcp_manager) if domain.mcp_manager is not None else None
        ),
        skill_tools=domain.skill_tool_provider,
        skill_drafts=domain.skill_draft_assistant,
        skill_learner=domain.skill_learner,
        web_fetch=domain.web_fetch_tool,
        cognitive_cycle=domain.cognitive_cycle,
        avatar_store=domain.avatar_store,
        goal_tracker=domain.goal_tracker,
        action_registry=domain.action_registry,
    )
