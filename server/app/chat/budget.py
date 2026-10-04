"""Bind a run budget to model calls without mutating cached routers."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from app.harness.budget import BudgetDenied, ModelBudget, budget_scope
from app.harness.guarded_call import guarded_call
from app.harness.model_accounting import ModelAccounting, model_accounting_scope
from app.llm.contracts import CompletionRequest, CompletionResult


class Backend(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...

    async def stream(
        self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
    ) -> CompletionResult: ...


class BudgetedBackend:
    def __init__(
        self,
        backend: Backend,
        budget: ModelBudget | None,
        *,
        validate: Callable[[], Awaitable[None]] | None = None,
        accounting: ModelAccounting | None = None,
    ) -> None:
        self._backend = backend
        self._budget = budget
        self._validate = validate
        self._accounting = accounting

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        with budget_scope(self._budget), model_accounting_scope(self._accounting):
            if self._validate is None:
                return await self._backend.complete(request)
            return await guarded_call(lambda: self._backend.complete(request), self._validate)

    async def stream(
        self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
    ) -> CompletionResult:
        with budget_scope(self._budget), model_accounting_scope(self._accounting):
            if self._validate is None:
                return await self._backend.stream(request, on_delta)
            active = True

            async def validate() -> None:
                nonlocal active
                try:
                    assert self._validate is not None
                    await self._validate()
                except BaseException:
                    active = False
                    raise

            async def delta(value: str) -> None:
                if not active:
                    raise BudgetDenied("budget_run_inactive")
                await on_delta(value)

            try:
                return await guarded_call(lambda: self._backend.stream(request, delta), validate)
            finally:
                active = False
