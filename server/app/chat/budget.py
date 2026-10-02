"""Bind a run budget to model calls without mutating cached routers."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from app.harness.budget import ModelBudget, budget_scope
from app.llm.contracts import CompletionRequest, CompletionResult


class Backend(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...

    async def stream(
        self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
    ) -> CompletionResult: ...


class BudgetedBackend:
    def __init__(self, backend: Backend, budget: ModelBudget | None) -> None:
        self._backend = backend
        self._budget = budget

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        with budget_scope(self._budget):
            return await self._backend.complete(request)

    async def stream(
        self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
    ) -> CompletionResult:
        with budget_scope(self._budget):
            return await self._backend.stream(request, on_delta)
