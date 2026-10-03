"""Application cleanup finishes on the caller before cancellation is reported."""

import asyncio

import pytest

from app.harness.budget import BudgetDenied
from app.harness.guarded_call import guarded_inline_call


async def test_application_call_keeps_owning_task_and_result_identity() -> None:
    owner, result = asyncio.current_task(), object()

    async def invoke() -> object:
        assert asyncio.current_task() is owner
        return result

    async def validate() -> None:
        return None

    assert await guarded_inline_call(invoke, validate) is result


async def test_external_cancellation_waits_for_transaction_cleanup() -> None:
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def invoke() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Longer than detached provider-call cleanup's bounded wait.
            await asyncio.sleep(0.35)
            cleaned.set()

    async def validate() -> None:
        return None

    task = asyncio.create_task(guarded_inline_call(invoke, validate))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert cleaned.is_set() and task.cancelling() == 1


@pytest.mark.parametrize("swallow", [False, True])
async def test_source_rejection_waits_for_cleanup_and_preserves_original_reason(
    swallow: bool,
) -> None:
    started, cleaned = asyncio.Event(), asyncio.Event()
    denied = BudgetDenied("synthetic_source_revoked")
    revoked = False

    async def validate() -> None:
        if revoked:
            raise denied

    async def invoke() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not swallow:
                raise
        finally:
            await asyncio.sleep(0.35)
            cleaned.set()
        return "unusable result"

    task = asyncio.create_task(guarded_inline_call(invoke, validate))
    await asyncio.wait_for(started.wait(), 1)
    revoked = True
    with pytest.raises(BudgetDenied) as caught:
        await asyncio.wait_for(task, 2)
    assert caught.value is denied and cleaned.is_set() and task.cancelling() == 0


async def test_external_cancel_during_revocation_cleanup_remains_external_cancel() -> None:
    started, cleaning, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    revoked = False

    async def validate() -> None:
        if revoked:
            raise BudgetDenied("synthetic_source_revoked")

    async def invoke() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaned.set()
                raise

    task = asyncio.create_task(guarded_inline_call(invoke, validate))
    await asyncio.wait_for(started.wait(), 1)
    revoked = True
    await asyncio.wait_for(cleaning.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert cleaned.is_set() and task.cancelling() == 1


async def test_initial_rejection_never_invokes_application() -> None:
    called = False

    async def invoke() -> None:
        nonlocal called
        called = True

    async def validate() -> None:
        raise BudgetDenied("synthetic_source_invalid")

    with pytest.raises(BudgetDenied, match="synthetic_source_invalid"):
        await guarded_inline_call(invoke, validate)
    assert not called
