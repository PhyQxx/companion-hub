"""Revalidate revocable evidence around a cooperative asynchronous call."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


def _consume(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def guarded_call(
    invoke: Callable[[], Awaitable[T]], validate: Callable[[], Awaitable[None]]
) -> T:
    await validate()

    async def watch() -> None:
        while True:
            await asyncio.sleep(0.25)
            await validate()

    async def call() -> T:
        return await invoke()

    operation, watcher = asyncio.create_task(call()), asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait({operation, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            await watcher
        result = await operation
        await validate()
        return result
    finally:
        for task in (operation, watcher):
            if not task.done():
                task.cancel()
        await asyncio.wait({operation, watcher}, timeout=0.25)
        for task in (operation, watcher):
            task.add_done_callback(_consume)
