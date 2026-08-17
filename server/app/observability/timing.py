from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol
from uuid import UUID

from .redaction import redact_fields


@dataclass(frozen=True, slots=True)
class SpanRecord:
    trace_id: UUID
    name: str
    duration_ms: float
    success: bool
    attributes: dict[str, Any]
    error_type: str | None = None


class SpanSink(Protocol):
    async def emit(self, record: SpanRecord) -> None: ...


class InMemorySpanSink:
    def __init__(self) -> None:
        self.records: list[SpanRecord] = []

    async def emit(self, record: SpanRecord) -> None:
        self.records.append(record)


class TraceRecorder:
    def __init__(self, sink: SpanSink) -> None:
        self._sink = sink

    @asynccontextmanager
    async def span(
        self,
        trace_id: UUID,
        name: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[None]:
        started = perf_counter()
        error_type = None
        success = False
        try:
            yield
            success = True
        except BaseException as error:
            error_type = type(error).__name__
            raise
        finally:
            await self._sink.emit(
                SpanRecord(
                    trace_id=trace_id,
                    name=name,
                    duration_ms=(perf_counter() - started) * 1_000,
                    success=success,
                    attributes=redact_fields(attributes or {}),
                    error_type=error_type,
                )
            )
