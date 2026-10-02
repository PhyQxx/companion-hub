from __future__ import annotations

import asyncio
from time import perf_counter

from pydantic import ValidationError

from app.harness.budget import BudgetDenied, current_tool_budget
from app.llm import ToolCall
from app.privacy import EgressBlocked, EgressDestination, EgressGuard
from app.privacy.service import PolicyService
from app.schemas import PrivacyLevel

from .contracts import ToolContext, ToolExecution, ToolResult
from .ledger import ToolLedger
from .registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, *, egress: EgressGuard | None = None) -> None:
        self._registry = registry
        self._egress = egress or EgressGuard()
        self._policy = PolicyService(egress=self._egress)

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolExecution:
        started = perf_counter()
        handler = self._registry.get(call.function.name)
        if handler is None:
            return ToolExecution(
                call_id=call.id,
                result=self._failure(call.function.name, "tool_not_found", started),
            )
        try:
            self._policy.authorize(
                context.privacy_level,
                EgressDestination(
                    name=handler.name,
                    runs_local=bool(getattr(handler, "runs_local", False)),
                    max_privacy_level=PrivacyLevel(
                        getattr(handler, "max_privacy_level", PrivacyLevel.L1)
                    ),
                ),
                phase="tool",
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
        budget = current_tool_budget()
        permit = None
        if budget is not None:
            try:
                permit = await budget.reserve_tool(tool_name=handler.name, user_id=context.user_id)
            except BudgetDenied as error:
                return ToolExecution(
                    call_id=call.id, result=self._failure(handler.name, error.reason_code, started)
                )
        try:
            if permit is not None:
                async with asyncio.timeout(permit.remaining_seconds):
                    result = await handler.execute(arguments, context)
            else:
                result = await handler.execute(arguments, context)
        except BaseException:
            if budget is not None and permit is not None:
                await budget.settle_tool(permit.call_id, reported_ok=None)
            raise
        result = result.model_copy(update={"admission_status": "admitted"})
        if budget is not None and permit is not None:
            await budget.settle_tool(permit.call_id, reported_ok=result.ok)
        if result.provider in {"amap", "home_assistant"}:
            ToolLedger().record(result)
        return ToolExecution(call_id=call.id, result=result)

    @staticmethod
    def _failure(tool_name: str, reason_code: str, started: float) -> ToolResult:
        return ToolResult(
            admission_status="not_admitted",
            ok=False,
            tool_name=tool_name,
            reason_code=reason_code,
            latency_ms=(perf_counter() - started) * 1_000,
        )
