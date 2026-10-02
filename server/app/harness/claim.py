"""Request-local fencing identity, independent of job storage and providers."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from .budget import BudgetDenied


class ClaimInvalidated(BudgetDenied):
    def __init__(self) -> None:
        super().__init__("job_claim_lost")


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    job_id: UUID
    worker_id: str
    version: int


_CURRENT: ContextVar[ExecutionClaim | None] = ContextVar("execution_claim", default=None)


def current_claim() -> ExecutionClaim | None:
    return _CURRENT.get()


@contextmanager
def claim_scope(claim: ExecutionClaim) -> Iterator[None]:
    token = _CURRENT.set(claim)
    try:
        yield
    finally:
        _CURRENT.reset(token)
