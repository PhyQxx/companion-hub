from __future__ import annotations

import json
import logging

import pytest

from app.ids import uuid7
from app.observability import InMemorySpanSink, StructuredLogger, TraceRecorder


def test_structured_logger_redacts_payload_fields(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("aria.test")
    structured = StructuredLogger(logger)
    with caplog.at_level(logging.INFO, logger="aria.test"):
        structured.emit(
            logging.INFO,
            "model.completed",
            fields={"endpoint": "cloud", "prompt": "sensitive", "nested": {"token": "secret"}},
        )

    payload = json.loads(caplog.records[0].message)
    assert payload == {
        "endpoint": "cloud",
        "event": "model.completed",
        "nested": {"token": "[REDACTED]"},
        "prompt": "[REDACTED]",
    }


async def test_span_records_duration_and_payload_free_error() -> None:
    sink = InMemorySpanSink()
    recorder = TraceRecorder(sink)
    with pytest.raises(RuntimeError, match="sensitive exception detail"):
        async with recorder.span(uuid7(), "test", {"content": "secret", "route": "utility"}):
            raise RuntimeError("sensitive exception detail")

    record = sink.records[0]
    assert record.success is False
    assert record.error_type == "RuntimeError"
    assert record.duration_ms >= 0
    assert record.attributes == {"content": "[REDACTED]", "route": "utility"}
    assert "sensitive exception detail" not in repr(record)
