"""Keep an owned provider operation alive through lazy stream consumption."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar
from uuid import UUID

from app.config.models import RunBudgetConfig
from app.db import Database
from app.harness.operations import OperationPolicy
from app.harness.source_cleanup import close_after_source
from app.schemas import PrivacyLevel

from .operation import operate_with_run

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _Chunk(Generic[T]):
    value: T


@dataclass(frozen=True, slots=True)
class _Terminal:
    error: BaseException | None


async def _close_stream(stream: AsyncIterator[object]) -> None:
    close = getattr(stream, "aclose", None)
    if close is not None:
        await close()


async def _join_producer(task: asyncio.Task[None], *, cancel: bool) -> None:
    owner = asyncio.current_task()
    assert owner is not None
    initial_cancellations = owner.cancelling()
    if cancel and not task.done() and not task.cancelling():
        task.cancel()
    # Consumer close must wait for the operation's SQL/stream cleanup. Further
    # caller cancellation is recorded without cancelling that cleanup again.
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    error = None if task.cancelled() else task.exception()
    if owner.cancelling() > initial_cancellations:
        raise asyncio.CancelledError
    if error is not None:
        raise error


async def stream_with_run(
    database: Database,
    policy: OperationPolicy,
    *,
    user_id: UUID,
    privacy_level: PrivacyLevel,
    entry: str,
    create_stream: Callable[[], AsyncIterator[T]],
    evidence: Callable[[], dict[str, str]],
    source_guard: Callable[[], Awaitable[None]],
    cost_endpoint: str,
    budget_source: Callable[[], RunBudgetConfig] | None = None,
    trace_parent_id: UUID | None = None,
    missing_quote_reason: Literal[
        "media_cost_estimate_unavailable", "voice_cost_estimate_unavailable"
    ] = "media_cost_estimate_unavailable",
) -> AsyncGenerator[T, None]:
    """One provider dispatch, bounded handoff, terminal errors without replay.

    The existing operation owns admission, quote, parent budget and deadline.
    Its success means provider iteration and close finished, independently of
    downstream audio delivery. Closing this iterator joins its producer before
    returning; a never-consumed iterator creates no Run or fee.
    """
    queue: asyncio.Queue[_Chunk[T] | _Terminal] = asyncio.Queue(maxsize=1)
    closing = False
    finishing = False

    async def invoke(start: Callable[[], Awaitable[None]]) -> dict[str, str]:
        await start()
        stream = create_stream()

        async def close() -> None:
            nonlocal finishing
            # Once the provider has ended, consumer close joins its cleanup
            # and SQL result instead of injecting a new cancellation there.
            finishing = True
            await _close_stream(stream)

        async with close_after_source(close):
            async for chunk in stream:
                await queue.put(_Chunk(chunk))
        return evidence()

    async def produce() -> None:
        try:
            await operate_with_run(
                database,
                policy,
                user_id=user_id,
                privacy_level=privacy_level,
                entry=entry,
                invoke=invoke,
                evidence=lambda result: result,
                source_guard=source_guard,
                cost_endpoint=cost_endpoint,
                budget_source=budget_source,
                cooperative=True,
                trace_parent_id=trace_parent_id,
                missing_quote_reason=missing_quote_reason,
            )
        except BaseException as error:
            if closing:
                raise
            terminal = _Terminal(error)
        else:
            terminal = _Terminal(None)
        if not closing:
            await queue.put(terminal)

    producer = asyncio.create_task(produce(), name="owned-provider-stream")

    async def close_producer() -> None:
        nonlocal closing
        closing = True
        await _join_producer(producer, cancel=not finishing)

    async with close_after_source(close_producer):
        while True:
            item = await queue.get()
            if isinstance(item, _Terminal):
                if item.error is not None:
                    raise item.error
                return
            # Queued data may outlive the permission check that produced it.
            await source_guard()
            yield item.value
