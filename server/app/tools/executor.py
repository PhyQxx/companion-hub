from __future__ import annotations

from time import perf_counter

from pydantic import ValidationError

from app.llm import ToolCall
from app.privacy import EgressBlocked, EgressDestination, EgressGuard
from app.schemas import PrivacyLevel

from .contracts import ToolContext, ToolExecution, ToolResult
from .ledger import ToolLedger
from .registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, *, egress: EgressGuard | None = None) -> None:
        self._registry = registry
        self._egress = egress or EgressGuard()

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolExecution:
        started = perf_counter()
        handler = self._registry.get(call.function.name)
        if handler is None:
            return ToolExecution(
                call_id=call.id,
                result=self._failure(call.function.name, "tool_not_found", started),
            )
        try:
            self._egress.authorize(
                context.privacy_level,
                EgressDestination(
                    name=handler.name,
                    runs_local=bool(getattr(handler, "runs_local", False)),
                    max_privacy_level=PrivacyLevel(
                        getattr(handler, "max_privacy_level", PrivacyLevel.L1)
                    ),
                ),
            )
        except EgressBlocked:
            return ToolExecution(
                call_id=call.id,
                result=self._failure(handler.name, "egress_blocked", started),
            )
        try:
            arguments = handler.arguments_model.model_validate(call.function.arguments)
        except ValidationError:
            return ToolExecution(
                call_id=call.id,
                result=self._failure(handler.name, "tool_arguments_invalid", started),
            )
        result = await handler.execute(arguments, context)
        if result.provider == "amap":
            ToolLedger().record(result)
        return ToolExecution(call_id=call.id, result=result)

    @staticmethod
    def _failure(tool_name: str, reason_code: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            reason_code=reason_code,
            latency_ms=(perf_counter() - started) * 1_000,
        )
