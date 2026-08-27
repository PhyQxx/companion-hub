from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections import deque
from collections.abc import Mapping
from threading import Lock
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .redaction import redact_fields

if TYPE_CHECKING:
    from app.config.models import HubConfig

_APP_LOGGER = "app"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_LOG_BROADCAST_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


class LogBroadcastHandler(logging.Handler):
    """将日志记录广播到所有已连接的 SSE 客户端。

    使用线程安全的 deque 保存最近 2000 行历史，并通过 asyncio.Queue
    推送给每个订阅者。emit 可能在任意线程被调用，因此通过
    loop.call_soon_threadsafe 将写入委托到事件循环线程。
    """

    def __init__(self, max_history: int = 2000) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter(_LOG_BROADCAST_FORMAT))
        self._history: deque[str] = deque(maxlen=max_history)
        self._clients: list[asyncio.Queue[str]] = []
        self._lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            return
        with self._lock:
            self._history.append(line)
            clients = self._clients.copy()
        loop = self._loop
        if loop is not None and loop.is_running():
            for client in clients:
                with contextlib.suppress(Exception):
                    loop.call_soon_threadsafe(client.put_nowait, line)
        else:
            for client in clients:
                with contextlib.suppress(Exception):
                    client.put_nowait(line)

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        with self._lock:
            for line in self._history:
                try:
                    queue.put_nowait(line)
                except asyncio.QueueFull:
                    break
            self._clients.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        with self._lock:
            if queue in self._clients:
                self._clients.remove(queue)

    def history(self, limit: int = 200) -> list[str]:
        with self._lock:
            return list(self._history)[-limit:]


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


_BROADCAST_HANDLER: LogBroadcastHandler | None = None


def get_log_broadcast_handler() -> LogBroadcastHandler | None:
    return _BROADCAST_HANDLER


def configure_logging(level: str | int = "INFO") -> LogBroadcastHandler | None:
    """为 app.* 日志树配置统一的 stderr 输出与等级，并返回广播 handler。

    uvicorn 只配置 uvicorn.* 自身的记录器；若不在此处挂 handler，
    应用日志会传播到没有 handler 的根记录器，仅 WARNING 以上经
    Python 兜底输出，INFO/DEBUG 全部丢失——这正是聊天时看不到
    大模型调用日志的原因。
    """
    global _BROADCAST_HANDLER
    logger = logging.getLogger(_APP_LOGGER)
    logger.setLevel(_resolve_level(level))
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(handler)
    if _BROADCAST_HANDLER is None:
        _BROADCAST_HANDLER = LogBroadcastHandler()
        logger.addHandler(_BROADCAST_HANDLER)
    return _BROADCAST_HANDLER


def apply_observability(config: HubConfig) -> None:
    """把配置中的 observability.log_level 应用到 app 日志树。

    ARIA_LOG_LEVEL 环境变量存在时优先于配置，便于容器与调试器覆盖。
    """
    env_level = os.getenv("ARIA_LOG_LEVEL")
    configure_logging(env_level if env_level else config.observability.log_level)
