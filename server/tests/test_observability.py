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


@pytest.mark.parametrize("encoded", [False, True])
def test_oauth_callback_query_is_redacted_in_uvicorn_access_args(encoded: bool) -> None:
    from app.observability.logging import OAuthQueryFilter

    path = "/api/v1/calendar/google/%63allback" if encoded else "/api/v1/calendar/google/callback"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", path + "?code=synthetic-code&state=synthetic-state", "1.1", 200),
        None,
    )
    assert OAuthQueryFilter().filter(record)
    assert (
        "synthetic-code" not in record.getMessage() and "synthetic-state" not in record.getMessage()
    )
    assert "[REDACTED]" in record.getMessage()


def test_oauth_callback_query_is_redacted_in_httpx_url_object() -> None:
    import httpx

    from app.observability.logging import OAuthQueryFilter

    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        "",
        0,
        "HTTP Request: %s %s %d",
        (
            "GET",
            httpx.URL(
                "https://hub.example.test/api/v1/calendar/google/callback?code=synthetic-code&state=synthetic-state"
            ),
            200,
        ),
        None,
    )
    assert OAuthQueryFilter().filter(record)
    assert (
        "synthetic-code" not in record.getMessage() and "synthetic-state" not in record.getMessage()
    )
    assert "[REDACTED]" in record.getMessage()


def test_oauth_filter_preserves_ordinary_access_arguments() -> None:
    from app.observability.logging import OAuthQueryFilter

    args = ("127.0.0.1", "GET", "/health?ready=1", "1.1", 200)
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', args, None
    )
    OAuthQueryFilter().filter(record)
    assert record.args == args
