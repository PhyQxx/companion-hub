"""Request-local source identity, independent of model and tool quota ports."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.schemas import PrivacyLevel


@dataclass(frozen=True, slots=True)
class RunTraceSource:
    run_id: UUID
    parent_run_id: UUID | None
    conversation_id: UUID | None
    privacy_level: PrivacyLevel
    fingerprint: str
    allow_succeeded: bool


@dataclass(frozen=True, slots=True)
class DisabledRunTrace:
    database_key: int
    user_id: UUID
    sources: tuple[RunTraceSource, ...]
    expires_at: datetime | None = None

    @property
    def run_id(self) -> UUID:
        return self.sources[-1].run_id


_RUN_TRACE: ContextVar[DisabledRunTrace | None] = ContextVar("disabled_run_trace", default=None)


def current_run_trace() -> DisabledRunTrace | None:
    return _RUN_TRACE.get()


@contextmanager
def run_trace_scope(trace: DisabledRunTrace | None) -> Iterator[None]:
    token = _RUN_TRACE.set(trace)
    try:
        yield
    finally:
        _RUN_TRACE.reset(token)
