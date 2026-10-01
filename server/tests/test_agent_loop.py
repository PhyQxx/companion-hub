from __future__ import annotations

import pytest

from app.harness.loop import CompletionFrame, run_agent_loop
from app.ids import uuid7
from app.llm.contracts import (
    CompletionRequest,
    CompletionResult,
    LLMMessage,
    ToolCall,
    ToolDefinition,
)


def request() -> CompletionRequest:
    return CompletionRequest(
        trace_id=uuid7(),
        privacy_level="L1",
        route="dialogue",
        messages=[LLMMessage(role="user", content="question")],
        tools=[ToolDefinition(name="lookup", description="lookup", parameters={})],
    )


def result(*, calls: bool) -> CompletionResult:
    return CompletionResult(
        text="speculative" if calls else "verified answer",
        provider="test",
        model="test",
        endpoint="test",
        route="dialogue",
        latency_ms=0,
        tool_calls=[ToolCall(id="call", function={"name": "lookup", "arguments": {}})]
        if calls
        else [],
    )


def followup(
    current: CompletionRequest,
    completion: CompletionResult,
    executions: list[str],
    allow_tools: bool,
) -> CompletionRequest:
    return current.model_copy(
        update={
            "tools": current.tools if allow_tools else [],
            "tool_choice": "auto" if allow_tools else "none",
        }
    )


@pytest.mark.parametrize("streaming", [False, True])
async def test_round_limit_is_shared_and_speculative_chunks_are_hidden(streaming: bool) -> None:
    modes: list[bool] = []
    invoked: list[str] = []
    visible: list[str] = []

    async def complete(current: CompletionRequest, final: bool) -> CompletionFrame:
        modes.append(final)
        completion = result(calls=not final)
        return CompletionFrame(completion, (completion.text,) if streaming else ())

    async def execute(calls: list[ToolCall]) -> list[str]:
        invoked.extend(call.function.name for call in calls)
        return ["receipt"]

    async def deliver(frame: CompletionFrame) -> None:
        visible.extend(frame.buffered_chunks)

    outcome = await run_agent_loop(
        request(),
        max_tool_rounds=2,
        complete=complete,
        normalize=lambda current, completion: completion,
        execute=execute,
        followup=followup,
        direct_reply=lambda executions: None,
        check_cancelled=lambda: None,
        deliver=deliver,
    )
    assert modes == [False, False, True]
    assert invoked == ["lookup", "lookup"]
    assert outcome.executions == ("receipt", "receipt")
    assert outcome.request.tools == [] and outcome.request.tool_choice == "none"
    assert visible == (["verified answer"] if streaming else [])


@pytest.mark.parametrize("during", ["provider", "tool"])
async def test_cancellation_prevents_next_tool_or_completion(during: str) -> None:
    cancelled = False
    model_calls = 0
    tool_calls = 0
    visible: list[str] = []

    def check() -> None:
        if cancelled:
            raise RuntimeError("cancelled")

    async def complete(current: CompletionRequest, final: bool) -> CompletionFrame:
        nonlocal cancelled, model_calls
        model_calls += 1
        cancelled = during == "provider"
        return CompletionFrame(result(calls=True), ("speculative",))

    async def execute(calls: list[ToolCall]) -> list[str]:
        nonlocal cancelled, tool_calls
        tool_calls += 1
        cancelled = True
        return ["receipt"]

    async def deliver(frame: CompletionFrame) -> None:
        visible.extend(frame.buffered_chunks)

    with pytest.raises(RuntimeError, match="cancelled"):
        await run_agent_loop(
            request(),
            max_tool_rounds=3,
            complete=complete,
            normalize=lambda current, completion: completion,
            execute=execute,
            followup=followup,
            direct_reply=lambda executions: "receipt reply",
            check_cancelled=check,
            deliver=deliver,
        )
    assert model_calls == 1
    assert tool_calls == (0 if during == "provider" else 1)
    assert visible == []


async def test_direct_receipt_ends_loop_without_extra_model_call() -> None:
    visible: list[str] = []
    model_calls = 0

    async def complete(current: CompletionRequest, final: bool) -> CompletionFrame:
        nonlocal model_calls
        model_calls += 1
        return CompletionFrame(result(calls=True), ("speculative",))

    async def execute(calls: list[ToolCall]) -> list[str]:
        return ["receipt"]

    async def deliver(frame: CompletionFrame) -> None:
        visible.extend(frame.buffered_chunks)

    outcome = await run_agent_loop(
        request(),
        max_tool_rounds=3,
        complete=complete,
        normalize=lambda current, completion: completion,
        execute=execute,
        followup=followup,
        direct_reply=lambda executions: "receipt reply",
        check_cancelled=lambda: None,
        deliver=deliver,
    )
    assert model_calls == 1
    assert visible == ["receipt reply"]
    assert outcome.result.text == "receipt reply" and outcome.result.tool_calls == []


@pytest.mark.parametrize("has_tools", [False, True])
async def test_unsolicited_final_calls_never_execute(has_tools: bool) -> None:
    current = request()
    if not has_tools:
        current = current.model_copy(update={"tools": []})
    executed: list[str] = []

    async def complete(current: CompletionRequest, final: bool) -> CompletionFrame:
        # Faulty provider ignores tool_choice=none even on the final call.
        return CompletionFrame(result(calls=True))

    async def execute(calls: list[ToolCall]) -> list[str]:
        executed.extend(call.id for call in calls)
        return ["receipt"]

    outcome = await run_agent_loop(
        current,
        max_tool_rounds=1,
        complete=complete,
        normalize=lambda current, completion: completion,
        execute=execute,
        followup=followup,
        direct_reply=lambda executions: None,
        check_cancelled=lambda: None,
    )
    assert len(executed) == (1 if has_tools else 0)
    assert outcome.result.tool_calls == []
