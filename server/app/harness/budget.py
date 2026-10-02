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
