"""Request-local financial accounting, independent of model quota enforcement."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol
from uuid import UUID

from app.llm.contracts import ModelPricing, ModelUsage
from app.schemas import PrivacyLevel

from .budget import CallPermit


class ModelAccounting(Protocol):
    async def reserve(
        self, *, endpoint: str, tokens: int, pricing: ModelPricing, privacy_level: PrivacyLevel
    ) -> CallPermit: ...

    async def settle(self, call_id: UUID, usage: ModelUsage | None) -> None: ...


_ACCOUNTING: ContextVar[ModelAccounting | None] = ContextVar("model_accounting", default=None)


def current_model_accounting() -> ModelAccounting | None:
    return _ACCOUNTING.get()


@contextmanager
def model_accounting_scope(accounting: ModelAccounting | None) -> Iterator[None]:
    token = _ACCOUNTING.set(accounting)
    try:
        yield
    finally:
        _ACCOUNTING.reset(token)
