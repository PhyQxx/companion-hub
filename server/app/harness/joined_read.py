"""A cancelled source check joins acquisition and release before returning."""

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def joined_read(read: Awaitable[T]) -> T:
    return await join_on_cancel(read, name="joined-source-read")


async def join_on_cancel(operation: Awaitable[T], *, name: str) -> T:
    """Let owned cleanup finish; repeated caller cancellation does not forward."""

    async def invoke() -> T:
        return await operation

    task = asyncio.create_task(invoke(), name=name)
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
