from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config.store import load_config_file
from app.ids import uuid7
from app.observability import (
    InMemorySpanSink,
    StructuredLogger,
    TraceRecorder,
    apply_observability,
    configure_logging,
)


@pytest.fixture
def preserve_app_logger() -> Iterator[logging.Logger]:
    """configure_logging 会改动全局 app 记录器，测试后恢复现场。"""
    logger = logging.getLogger("app")
    level, handlers = logger.level, list(logger.handlers)
    yield logger
    logger.setLevel(level)
    logger.handlers[:] = handlers


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


def test_configure_logging_sets_level_once_and_falls_back(
    preserve_app_logger: logging.Logger,
) -> None:
    configure_logging("WARNING")
    assert preserve_app_logger.level == logging.WARNING
    assert preserve_app_logger.handlers, "应至少挂载一个输出 handler"

    handler_count = len(preserve_app_logger.handlers)
    configure_logging("DEBUG")
    assert preserve_app_logger.level == logging.DEBUG
    assert len(preserve_app_logger.handlers) == handler_count, "重复调用不得重复挂 handler"

    configure_logging("not-a-level")
    assert preserve_app_logger.level == logging.INFO, "非法等级应回退 INFO"


def test_apply_observability_follows_config_and_env_override(
    preserve_app_logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = load_config_file(Path(__file__).resolve().parents[2] / "config/hub.example.yaml")
    monkeypatch.delenv("ARIA_LOG_LEVEL", raising=False)
    apply_observability(config)
    assert preserve_app_logger.level == logging.getLevelName(config.observability.log_level)

    monkeypatch.setenv("ARIA_LOG_LEVEL", "ERROR")
    apply_observability(config)
    assert preserve_app_logger.level == logging.ERROR
