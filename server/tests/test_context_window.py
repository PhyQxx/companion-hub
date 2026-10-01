from __future__ import annotations

import pytest

from app.harness.context import ContextAssembler, ContextBlocks
from app.harness.window import ContextWindowExceeded, fit_window
from app.ids import uuid7
from app.llm import CompletionRequest, LLMRoute, ModelEndpoint, ToolCall
from app.llm.contracts import LLMMessage


def endpoint(window: int) -> ModelEndpoint:
    return ModelEndpoint(
        provider="test",
        model="test",
        base_url="http://localhost:8080",
        runs_local=True,
        max_privacy_level="L2",
        max_context_tokens=window,
        reasoning_overhead_tokens=100,
        max_tokens=100,
    )


def request(messages: list[LLMMessage]) -> CompletionRequest:
    return CompletionRequest(
        trace_id=uuid7(), messages=messages, privacy_level="L1", route=LLMRoute.DIALOGUE
    )


def test_window_preserves_exact_facts_and_complete_current_tool_chain() -> None:
    call = ToolCall(id="c", function={"name": "lookup", "arguments": {}})
    messages = [
        LLMMessage(role="system", content="exact fact: 987654321"),
        LLMMessage(role="user", content="old" * 1000),
        LLMMessage(role="assistant", tool_calls=[call]),
        LLMMessage(role="tool", name="lookup", tool_call_id="c", content="old result"),
        LLMMessage(role="user", content="current request"),
        LLMMessage(role="assistant", tool_calls=[call]),
        LLMMessage(role="tool", name="lookup", tool_call_id="c", content="current result"),
    ]
    original = request(messages)
    fitted = fit_window(original, endpoint(1800))
    assert fitted.request.messages == [messages[0], *messages[4:]]
    assert fitted.manifest["excluded_messages"] == 3
    assert fitted.manifest["reasoning_tokens"] == 100
    assert fitted.request.max_tokens == 100
    assert original.messages == messages
    assert fit_window(original, endpoint(10000)).request.messages == messages


def test_window_rejects_oversized_protected_context() -> None:
    with pytest.raises(ContextWindowExceeded, match="context_window_exceeded"):
        fit_window(request([LLMMessage(role="user", content="current" * 1000)]), endpoint(1000))


def test_source_manifest_contains_no_content() -> None:
    manifest = ContextAssembler().source_manifest(
        blocks=ContextBlocks(time="private time", reality="private reality", memory="secret fact"),
        history=[],
        conversation_summary=None,
        owner_id="owner",
        privacy_level="L1",
        summary_exclusion_reason="privacy_filtered",
    )
    assert "secret fact" not in str(manifest)
    assert manifest[-1]["reason"] == "privacy_filtered"
    assert next(source for source in manifest if source["source"] == "memory")["included"]


def test_tool_schema_consumes_window_budget() -> None:
    from app.llm import ToolDefinition

    original = request([LLMMessage(role="user", content="current")])
    tools = [
        ToolDefinition(
            name="lookup",
            description="schema" * 300,
            parameters={"type": "object", "properties": {}},
        )
    ]
    original = original.model_copy(update={"tools": tools})
    with pytest.raises(ContextWindowExceeded):
        fit_window(original, endpoint(1600))


def test_optional_summary_is_removed_without_rewriting_protected_facts() -> None:
    from app.llm.contracts import LLMContextPart

    parts = [
        LLMContextPart(source="policy", content="policy and exact fact 123456"),
        LLMContextPart(
            source="conversation_summary", content="\n\n" + "old" * 500, protected=False
        ),
    ]
    original = request(
        [
            LLMMessage(role="system", content="".join(part.content for part in parts)),
            LLMMessage(role="user", content="current"),
        ]
    ).model_copy(update={"context_parts": parts})
    fitted = fit_window(original, endpoint(1000))
    assert fitted.request.messages[0].content == parts[0].content
    assert fitted.manifest["excluded_sources"] == "conversation_summary"
    # A repair that adds an instruction invalidates the composition snapshot.
    repaired = original.model_copy(
        update={
            "messages": [
                original.messages[0].model_copy(
                    update={"content": original.messages[0].content + "repair"}
                ),
                original.messages[1],
            ]
        }
    )
    with pytest.raises(ContextWindowExceeded):
        fit_window(repaired, endpoint(1000))
