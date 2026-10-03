"""Ingress locks survive contention but do not retain every historical source."""

import asyncio
import gc

from test_cognitive_flow_ports import memory_pipeline
from test_perception import event

from app.ids import uuid7
from app.perception.models import PerceptionDisposition


async def test_completed_unique_sources_do_not_accumulate_idle_locks() -> None:
    pipeline, decisions, _, _, _ = memory_pipeline()
    owner = uuid7()
    for index in range(250):
        await pipeline.process(event(owner, "unknown_probe", dedupe_key=f"source:{index}"))
    gc.collect()
    assert len(decisions.saved) == 250
    assert not pipeline._locks


async def test_waiters_keep_one_lock_through_collection_and_waiter_cancellation() -> None:
    pipeline, decisions, world, _, _ = memory_pipeline()
    world.release.clear()
    owner = uuid7()
    first = asyncio.create_task(
        pipeline.process(event(owner, "unknown_probe", dedupe_key="contended"))
    )
    await asyncio.wait_for(world.entered.wait(), 1)
    waiters = [
        asyncio.create_task(pipeline.process(event(owner, "unknown_probe", dedupe_key="contended")))
        for _ in range(10)
    ]
    try:
        await asyncio.sleep(0)
        gc.collect()
        assert len(pipeline._locks) == 1
        waiters[0].cancel()
        await asyncio.gather(waiters[0], return_exceptions=True)
        gc.collect()
        assert len(pipeline._locks) == 1
        world.release.set()
        assert (await first).disposition == PerceptionDisposition.PROCESSED
        results = await asyncio.gather(*waiters[1:])
        assert all(value.disposition == PerceptionDisposition.MERGED for value in results)
        assert len(decisions.saved) == 1
    finally:
        world.release.set()
        for task in (first, *waiters):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, *waiters, return_exceptions=True)
    # A retained cancelled Task owns its exception traceback and local lock.
    # Drop caller-owned completed tasks before checking registry retention.
    del first, waiters, task
    await asyncio.sleep(0)  # Let gather/cancellation callbacks release completed tasks.
    gc.collect()
    assert not pipeline._locks


async def test_cancelled_holder_releases_the_lock_to_existing_waiters() -> None:
    pipeline, decisions, world, _, _ = memory_pipeline()
    world.release.clear()
    owner = uuid7()
    tasks = [
        asyncio.create_task(pipeline.process(event(owner, "unknown_probe", dedupe_key="cancelled")))
        for _ in range(3)
    ]
    try:
        await asyncio.wait_for(world.entered.wait(), 1)
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        gc.collect()
        assert len(pipeline._locks) == 1
        world.release.set()
        results = await asyncio.gather(*tasks[1:])
        assert [value.disposition for value in results] == [
            PerceptionDisposition.PROCESSED,
            PerceptionDisposition.MERGED,
        ]
        assert len(decisions.saved) == 1
    finally:
        world.release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    del tasks, task
    await asyncio.sleep(0)
    gc.collect()
    assert not pipeline._locks
