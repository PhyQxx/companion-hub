"""Revalidate revocable evidence around a cooperative asynchronous call."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
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


async def guarded_inline_call(
    invoke: Callable[[], Awaitable[T]],
    validate: Callable[[], Awaitable[None]],
    *,
    defer_watch: Callable[[], bool] | None = None,
) -> T:
    """Keep cooperative application transactions on their owning caller task.

    Unlike a detached provider call, application cancellation must finish its
    own transaction cleanup before the caller reports completion. Only the
    read-only authority watcher runs in a child task. An explicit shutdown
    marker can defer watcher cancellation during SDK cleanup; validation still
    runs after invoke returns, and the containing operation keeps its deadline.
    """
    await validate()
    owner = asyncio.current_task()
    assert owner is not None
    initial_cancellations = owner.cancelling()
    denied: Exception | None = None
    sent_cancellation = False

    async def watch() -> None:
        nonlocal denied, sent_cancellation
        try:
            while True:
                await asyncio.sleep(0.25)
                if defer_watch is not None and defer_watch():
                    continue
                await validate()
        except Exception as error:
            # An external cancellation already in progress keeps its meaning.
            if owner.cancelling() > initial_cancellations:
                return
            denied = error
            sent_cancellation = True
            owner.cancel()

    watcher = asyncio.create_task(watch())
    try:
        result = await invoke()
        if denied is not None:
            raise denied from None
        await validate()
        return result
    except asyncio.CancelledError:
        if denied is not None and owner.cancelling() == initial_cancellations + 1:
            owner.uncancel()
            sent_cancellation = False
            raise denied from None
        raise
    finally:
        if not watcher.done():
            watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
        if sent_cancellation:
            owner.uncancel()  # Remove only this watcher's cancellation request.
