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
    parent: "ExecutionClaim | None" = None


_CURRENT: ContextVar[ExecutionClaim | None] = ContextVar("execution_claim", default=None)


def current_claim() -> ExecutionClaim | None:
    return _CURRENT.get()


def current_claims() -> tuple[ExecutionClaim, ...]:
    claims: list[ExecutionClaim] = []
    claim = current_claim()
    while claim is not None:
        claims.append(claim)
        claim = claim.parent
    return tuple(reversed(claims))


@contextmanager
def claim_scope(claim: ExecutionClaim) -> Iterator[None]:
    from dataclasses import replace

    parent = current_claim()
    if parent is not None and parent != claim:
        claim = replace(claim, parent=parent)
    token = _CURRENT.set(claim)
    try:
        yield
    finally:
        _CURRENT.reset(token)
