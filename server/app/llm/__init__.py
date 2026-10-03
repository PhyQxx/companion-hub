"""Public llm exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .contracts import (
        CompletionRequest,
        CompletionResult,
        LLMMessage,
        LLMRoute,
        ModelEndpoint,
        ModelKind,
        ModelUsage,
        RoutePolicy,
        ToolCall,
        ToolDefinition,
        ToolFunction,
    )
    from .provider import EnvSecretProvider, LiteLLMProvider, LLMProvider, SecretNotFound
    from .router import LLMEndpointFailure, LLMRouteExhausted, LLMRouter
    from .text_tool_calls import extract_text_tool_calls

_EXPORTS = {
    "CompletionRequest": ("app.llm.contracts", "CompletionRequest"),
    "CompletionResult": ("app.llm.contracts", "CompletionResult"),
    "EnvSecretProvider": ("app.llm.provider", "EnvSecretProvider"),
    "LLMEndpointFailure": ("app.llm.router", "LLMEndpointFailure"),
    "LLMMessage": ("app.llm.contracts", "LLMMessage"),
    "LLMProvider": ("app.llm.provider", "LLMProvider"),
    "LLMRoute": ("app.llm.contracts", "LLMRoute"),
    "LLMRouteExhausted": ("app.llm.router", "LLMRouteExhausted"),
    "LLMRouter": ("app.llm.router", "LLMRouter"),
    "LiteLLMProvider": ("app.llm.provider", "LiteLLMProvider"),
    "ModelEndpoint": ("app.llm.contracts", "ModelEndpoint"),
    "ModelKind": ("app.llm.contracts", "ModelKind"),
    "ModelUsage": ("app.llm.contracts", "ModelUsage"),
    "RoutePolicy": ("app.llm.contracts", "RoutePolicy"),
    "SecretNotFound": ("app.llm.provider", "SecretNotFound"),
    "ToolCall": ("app.llm.contracts", "ToolCall"),
    "ToolDefinition": ("app.llm.contracts", "ToolDefinition"),
    "ToolFunction": ("app.llm.contracts", "ToolFunction"),
    "extract_text_tool_calls": ("app.llm.text_tool_calls", "extract_text_tool_calls"),
}

__all__ = [
    "CompletionRequest",
    "CompletionResult",
    "EnvSecretProvider",
    "LLMEndpointFailure",
    "LLMMessage",
    "LLMProvider",
    "LLMRoute",
    "LLMRouteExhausted",
    "LLMRouter",
    "LiteLLMProvider",
    "ModelEndpoint",
    "ModelKind",
    "ModelUsage",
    "RoutePolicy",
    "SecretNotFound",
    "ToolCall",
    "ToolDefinition",
    "ToolFunction",
    "extract_text_tool_calls",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
