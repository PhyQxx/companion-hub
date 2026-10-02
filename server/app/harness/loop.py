"""Bounded model/tool coordination through application-owned ports.

The loop owns round limits and cancellation checkpoints. Applications own
permissions, tool execution, output filtering, and domain-specific receipts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from app.llm.contracts import CompletionRequest, CompletionResult, ToolCall

Execution = TypeVar("Execution")


@dataclass(frozen=True, slots=True)
class CompletionFrame:
    result: CompletionResult
    buffered_chunks: tuple[str, ...] = ()
    effective_request: CompletionRequest | None = None


@dataclass(frozen=True, slots=True)
class LoopOutcome(Generic[Execution]):
    result: CompletionResult
    request: CompletionRequest
    executions: tuple[Execution, ...]


async def run_agent_loop(
    request: CompletionRequest,
    *,
    max_tool_rounds: int,
    complete: Callable[[CompletionRequest, bool], Awaitable[CompletionFrame]],
    normalize: Callable[[CompletionRequest, CompletionResult], CompletionResult],
    execute: Callable[[list[ToolCall]], Awaitable[list[Execution]]],
    followup: Callable[
        [CompletionRequest, CompletionResult, list[Execution], bool], CompletionRequest
    ],
    direct_reply: Callable[[list[Execution]], str | None],
    check_cancelled: Callable[[], None],
    deliver: Callable[[CompletionFrame], Awaitable[None]] | None = None,
) -> LoopOutcome[Execution]:
    if max_tool_rounds < 1:
        raise ValueError("invalid_tool_round_limit")
    executions: tuple[Execution, ...] = ()
    rounds_left = max_tool_rounds
    while True:
        check_cancelled()
        frame = await complete(request, not request.tools or rounds_left == 0)
        request = frame.effective_request or request
        result = normalize(request, frame.result)
        check_cancelled()
        if not request.tools or request.tool_choice == "none" or rounds_left == 0:
            # A provider can return unsolicited native calls despite the final
            # request. They are not an authorization to execute another round.
            result = result.model_copy(update={"tool_calls": []})
        if not result.tool_calls:
            if deliver is not None:
                await deliver(CompletionFrame(result, frame.buffered_chunks))
            return LoopOutcome(result, request, executions)
        # Speculative text from a tool-requesting completion is never delivered.
        current = await execute(result.tool_calls)
        executions = (*executions, *current)
        check_cancelled()
        rounds_left -= 1
        request = followup(request, result, current, rounds_left > 0)
        reply = direct_reply(current)
        if reply is not None:
            result = result.model_copy(
                update={"text": reply, "tool_calls": [], "finish_reason": "stop"}
            )
            if deliver is not None:
                await deliver(CompletionFrame(result, (reply,)))
            return LoopOutcome(result, request, executions)
