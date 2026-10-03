"""A cancelled source check joins acquisition and release before returning."""

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def joined_read(read: Awaitable[T]) -> T:
    async def invoke() -> T:
        return await read

    task = asyncio.create_task(invoke(), name="joined-source-read")
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise
