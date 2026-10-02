"""Request-local model budget port; no persistence or provider dependencies."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.llm.contracts import ModelUsage


class BudgetDenied(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class CallPermit:
    call_id: UUID
    remaining_seconds: float


class ModelBudget(Protocol):
    async def reserve(self, *, endpoint: str, tokens: int, final: bool) -> CallPermit: ...

    async def settle(self, call_id: UUID, usage: ModelUsage | None) -> None: ...


_CURRENT_BUDGET: ContextVar[ModelBudget | None] = ContextVar("model_budget", default=None)


def current_budget() -> ModelBudget | None:
    return _CURRENT_BUDGET.get()


@contextmanager
def budget_scope(budget: ModelBudget | None) -> Iterator[None]:
    token = _CURRENT_BUDGET.set(budget)
    try:
        yield
    finally:
        _CURRENT_BUDGET.reset(token)


@dataclass(frozen=True, slots=True)
class ToolPermit:
    call_id: UUID
    remaining_seconds: float


class ToolBudget(Protocol):
    async def reserve_tool(self, *, tool_name: str, user_id: UUID | None) -> ToolPermit: ...

    async def settle_tool(self, call_id: UUID, *, reported_ok: bool | None) -> None: ...


_TOOL_BUDGET: ContextVar[ToolBudget | None] = ContextVar("tool_budget", default=None)


def current_tool_budget() -> ToolBudget | None:
    explicit = _TOOL_BUDGET.get()
    if explicit is not None:
        return explicit
    budget = current_budget()
    from typing import cast

    return cast(ToolBudget | None, getattr(budget, "tool_budget", None))


@contextmanager
def tool_budget_scope(budget: ToolBudget | None) -> Iterator[None]:
    token = _TOOL_BUDGET.set(budget)
    try:
        yield
    finally:
        _TOOL_BUDGET.reset(token)
