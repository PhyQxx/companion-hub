"""Per-task extraction options, isolated across concurrent conversations."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_CONTEXT: ContextVar[str] = ContextVar("memory_extraction_context", default="")
_ERRORS: ContextVar[list[str] | None] = ContextVar("memory_extraction_errors", default=None)
_SOURCES: ContextVar[tuple[str, ...]] = ContextVar("memory_extraction_sources", default=())
_STRICT: ContextVar[bool] = ContextVar("memory_extraction_strict", default=False)


def extraction_context() -> str:
    return _CONTEXT.get()


def extraction_sources() -> tuple[str, ...]:
    return _SOURCES.get()


def strict_extraction() -> bool:
    return _STRICT.get()


def record_extraction_error(error: Exception) -> None:
    errors = _ERRORS.get()
    if errors is not None and len(errors) < 20:
        errors.append(type(error).__name__)


@contextmanager
def extraction_scope(
    context: str = "", *, strict: bool = False, source_ids: tuple[str, ...] = ()
) -> Iterator[list[str]]:
    errors: list[str] = []
    c = _ERRORS.set(errors)
    d = _SOURCES.set(source_ids)
    a, b = _CONTEXT.set(context), _STRICT.set(strict)
    try:
        yield errors
    finally:
        _ERRORS.reset(c)
        _SOURCES.reset(d)
        _CONTEXT.reset(a)
        _STRICT.reset(b)
