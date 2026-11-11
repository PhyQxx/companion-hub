"""Public chat exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from app.schemas.reply import AgentAction, AgentReply

    from .capabilities import (
        CompositeRuntimeCapabilityProvider,
        RuntimeActionCapability,
        RuntimeCapabilityProvider,
        render_reality_grounding,
    )
    from .memory_consistency import (
        MemoryConsistencyGuard,
        MemoryConsistencyOutcome,
    )
    from .models import (
        ChatTurn,
        CompletionBackend,
        ConversationView,
        MessageView,
        PendingTurn,
        TurnCancelled,
    )
    from .reply import ControlStreamFilter, parse_agent_reply
    from .service import ChatService

_EXPORTS = {
    "AgentAction": ("app.schemas.reply", "AgentAction"),
    "AgentReply": ("app.schemas.reply", "AgentReply"),
    "ChatService": ("app.chat.service", "ChatService"),
    "ChatTurn": ("app.chat.models", "ChatTurn"),
    "CompletionBackend": ("app.chat.models", "CompletionBackend"),
    "CompositeRuntimeCapabilityProvider": (
        "app.chat.capabilities",
        "CompositeRuntimeCapabilityProvider",
    ),
    "ControlStreamFilter": ("app.chat.reply", "ControlStreamFilter"),
    "ConversationView": ("app.chat.models", "ConversationView"),
    "MemoryConsistencyGuard": ("app.chat.memory_consistency", "MemoryConsistencyGuard"),
    "MemoryConsistencyOutcome": ("app.chat.memory_consistency", "MemoryConsistencyOutcome"),
    "MessageView": ("app.chat.models", "MessageView"),
    "PendingTurn": ("app.chat.models", "PendingTurn"),
    "RuntimeActionCapability": ("app.chat.capabilities", "RuntimeActionCapability"),
    "RuntimeCapabilityProvider": ("app.chat.capabilities", "RuntimeCapabilityProvider"),
    "TurnCancelled": ("app.chat.models", "TurnCancelled"),
    "parse_agent_reply": ("app.chat.reply", "parse_agent_reply"),
    "render_reality_grounding": ("app.chat.capabilities", "render_reality_grounding"),
}

__all__ = [
    "AgentAction",
    "AgentReply",
    "ChatService",
    "ChatTurn",
    "CompletionBackend",
    "CompositeRuntimeCapabilityProvider",
    "ControlStreamFilter",
    "ConversationView",
    "MemoryConsistencyGuard",
    "MemoryConsistencyOutcome",
    "MessageView",
    "PendingTurn",
    "RuntimeActionCapability",
    "RuntimeCapabilityProvider",
    "TurnCancelled",
    "parse_agent_reply",
    "render_reality_grounding",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
