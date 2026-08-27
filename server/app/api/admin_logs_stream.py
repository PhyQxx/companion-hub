from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.observability import LogBroadcastHandler, get_log_broadcast_handler
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard


class LogLineView(StrictModel):
    line: str


def create_logs_stream_router(*, admin_token: str | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/logs",
        tags=["admin-logs"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("/stream")
    async def stream_logs() -> StreamingResponse:
        """Server-Sent Events 端点：实时推送 app 日志流。"""
        handler = get_log_broadcast_handler()
        if handler is None:
            return StreamingResponse(
                _empty_stream(),
                media_type="text/event-stream",
            )

        return StreamingResponse(
            _log_event_stream(handler),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    @router.get("/history")
    async def log_history(limit: int = 200) -> list[LogLineView]:
        handler = get_log_broadcast_handler()
        if handler is None:
            return []
        return [LogLineView(line=line) for line in handler.history(limit=limit)]

    return router


async def _empty_stream() -> AsyncIterator[str]:
    yield "data: {}\n\n"
    await asyncio.sleep(60)


async def _log_event_stream(handler: LogBroadcastHandler) -> AsyncIterator[str]:
    queue = handler.subscribe()
    try:
        while True:
            line = await queue.get()
            payload = json.dumps({"line": line}, ensure_ascii=False)
            yield f"data: {payload}\n\n"
    finally:
        handler.unsubscribe(queue)
