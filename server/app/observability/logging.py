# ruff: noqa: RUF002, RUF003
from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .redaction import redact_fields

if TYPE_CHECKING:
    from app.config.models import HubConfig

_APP_LOGGER = "app"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class StructuredLogger:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def emit(
        self,
        level: int,
        event: str,
        *,
        trace_id: UUID | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {"event": event}
        if trace_id is not None:
            record["trace_id"] = str(trace_id)
        record.update(redact_fields(fields or {}))
        self._logger.log(level, json.dumps(record, sort_keys=True, default=str))


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(level.strip().upper())
    # 未识别的等级名会返回字符串说明，此时回退 INFO 保证日志链路可用
    return resolved if isinstance(resolved, int) else logging.INFO


def configure_logging(level: str | int = "INFO") -> None:
    """为 app.* 日志树配置统一的 stderr 输出与等级。

    uvicorn 只配置 uvicorn.* 自身的记录器；若不在此处挂 handler，
    应用日志会传播到没有 handler 的根记录器，仅 WARNING 以上经
    Python 兜底输出，INFO/DEBUG 全部丢失——这正是聊天时看不到
    大模型调用日志的原因。
    """
    logger = logging.getLogger(_APP_LOGGER)
    logger.setLevel(_resolve_level(level))
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(handler)


def apply_observability(config: HubConfig) -> None:
    """把配置中的 observability.log_level 应用到 app 日志树。

    ARIA_LOG_LEVEL 环境变量存在时优先于配置，便于容器与调试器覆盖。
    """
    env_level = os.getenv("ARIA_LOG_LEVEL")
    configure_logging(env_level if env_level else config.observability.log_level)
