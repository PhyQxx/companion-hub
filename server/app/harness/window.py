"""Conservative, provider-independent window fitting without content rewriting."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version

from app.llm.contracts import CompletionRequest, LLMMessage, ModelEndpoint

from .tokenizer import count_tokens, local_tokenizer


class ContextWindowExceeded(ValueError):
    """Protected context cannot fit; callers may try another endpoint."""


@dataclass(frozen=True, slots=True)
class WindowFit:
    request: CompletionRequest
    manifest: dict[str, int | str]


def estimate_tokens(value: str) -> int:
    # UTF-8 bytes provide a conservative heuristic for mixed CJK/Latin input.
    # This is not an endpoint tokenizer; leave additional protocol headroom.
    return len(value.encode("utf-8"))


def fit_window(request: CompletionRequest, endpoint: ModelEndpoint) -> WindowFit:
    output = endpoint.max_tokens or request.max_tokens
    spec = endpoint.context_tokenizer
    if spec is not None:
        local_tokenizer(spec)  # Validate configured artifacts even for trivial input.
    protocol = spec.protocol_reserve_tokens if spec is not None else 256

    @lru_cache(maxsize=512)
    def count(value: str) -> int:
        return (
            math.ceil(count_tokens(value, spec) * spec.safety_multiplier)
            if spec is not None
            else estimate_tokens(value)
        )

    def message_cost(message: LLMMessage) -> int:
        return 16 + count(json.dumps(message.model_dump(exclude_none=True), ensure_ascii=False))

    reserve = output + endpoint.reasoning_overhead_tokens + protocol
    tool_cost = (
        count(json.dumps([tool.model_dump() for tool in request.tools], ensure_ascii=False))
        + 64 * len(request.tools)
        if request.tools
        else 0
    )
    budget = endpoint.max_context_tokens - reserve - tool_cost
    messages = list(request.messages)
    excluded_sources: list[str] = []
    parts = list(request.context_parts)
    # Repair/tool followups can alter the system prompt. Never rebuild from
    # stale metadata: retain that whole system message as protected instead.
    if (
        parts
        and messages[0].role == "system"
        and "".join(part.content for part in parts) == messages[0].content
    ):
        for part in sorted(
            (part for part in parts if not part.protected), key=lambda part: part.priority
        ):
            if sum(message_cost(m) for m in messages) <= budget:
                break
            parts.remove(part)
            excluded_sources.append(part.source)
            messages[0] = messages[0].model_copy(
                update={
                    "content": "".join(item.content for item in parts),
                }
            )
    # Keep every system constraint. The newest user and everything following it
    # form a protected unit (including all current tool calls/results).
    last_user = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"), 0)
    protected = [m for i, m in enumerate(messages) if m.role == "system" or i >= last_user]
    if sum(message_cost(m) for m in protected) > budget:
        raise ContextWindowExceeded("context_window_exceeded")
    # Remove oldest complete user turns, never individual tool messages. If an
    # unusual caller supplies a leading assistant/tool prefix, remove it as one.
    removed = 0
    while sum(message_cost(m) for m in messages) > budget:
        start = next(i for i, m in enumerate(messages) if m.role != "system" and i < last_user)
        end = next(
            (i for i in range(start + 1, last_user) if messages[i].role == "user"), last_user
        )
        indices = {i for i in range(start, end) if messages[i].role != "system"}
        messages = [m for i, m in enumerate(messages) if i not in indices]
        last_user -= len(indices)
        removed += len(indices)
    fitted = CompletionRequest.model_validate(
        {
            **request.model_dump(mode="python"),
            "messages": messages,
            "max_tokens": output,
            "context_parts": parts,
        }
    )
    return WindowFit(
        fitted,
        {
            "estimator": "native_json_estimate_v1" if spec is not None else "utf8_bytes_v1",
            "tokenizer_sha256": spec.sha256 if spec is not None else "",
            "tokenizer_library_version": version("tokenizers") if spec is not None else "",
            "provider_protocol_validation": "unverified",
            "excluded_sources": ",".join(excluded_sources),
            "window_tokens": endpoint.max_context_tokens,
            "estimated_input_tokens": sum(message_cost(m) for m in messages) + tool_cost,
            "output_tokens": output,
            "reasoning_tokens": endpoint.reasoning_overhead_tokens,
            "protocol_reserve_tokens": protocol,
            "excluded_messages": removed,
            "included_messages": len(messages),
        },
    )
