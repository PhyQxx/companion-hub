"""Hot disable and shutdown revoke work already awaiting private ports."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from test_cognitive_save_guard import database as save_database
from test_observation_inflight import fixture_loop

from app.context.observation import ObservationUnavailable
from app.db import Database, TimelineEventRecord

database = save_database


def disable(loop: Any, kind: str, config: Any) -> None:
    replacement = config.model_copy(update={"enabled": False})
    loop._config_store.current = SimpleNamespace(
        config=SimpleNamespace(**{f"{kind}_awareness": replacement})
    )


@pytest.mark.parametrize("kind", ["mail", "browser", "screen"])
@pytest.mark.parametrize("stage", ["read", "analysis"])
@pytest.mark.parametrize("mode", ["config", "stop"])
async def test_inflight_disable_and_stop_reject_results_without_provider_cooldown(
    database: Database, kind: str, stage: str, mode: str
) -> None:
    loop, config, _, ports = await fixture_loop(database, kind)
    ports.block_stage = stage
    if mode == "stop":
        loop.start()
        task = loop._task
    else:
        task = asyncio.create_task(loop._tick(config))
    assert task is not None
    try:
        await asyncio.wait_for(ports.started.wait(), 2)
        if mode == "stop":
            await asyncio.wait_for(loop.stop(), 2)
            assert not loop.state.running and loop._task is None
        else:
            disable(loop, kind, config)
        outcome = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2))[0]
        assert (
            outcome is None
            or isinstance(outcome, asyncio.CancelledError)
            or (
                isinstance(outcome, ObservationUnavailable)
                and outcome.reason_code == "observation_source_inactive"
            )
        )
        assert ports.cancelled.is_set()
        assert not ports.memories and not ports.events and not ports.deliveries
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(TimelineEventRecord)) == 0
        if kind == "screen":
            assert loop.state.displays[1].consecutive_failures == 0
            assert loop.state.displays[1].disabled_until is None
        else:
            assert loop.state.consecutive_failures == 0 and loop.state.cooldown_until is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await loop.stop()


@pytest.mark.parametrize("kind", ["mail", "browser", "screen"])
async def test_config_disabled_after_event_queued_rejects_validation_and_delivery(
    database: Database, kind: str
) -> None:
    loop, config, _, ports = await fixture_loop(database, kind)
    await loop._tick(config)
    assert len(ports.events) == 1
    event, validate, handler = ports.events[0]
    disable(loop, kind, config)
    assert not await validate()
    await handler(event, SimpleNamespace(decision=SimpleNamespace(decision="inform")))
    assert not ports.deliveries


@pytest.mark.parametrize("kind", ["mail", "browser", "screen"])
async def test_stop_waits_for_owned_direct_background_and_blocks_delayed_handler(
    database: Database, kind: str
) -> None:
    loop, config, _, ports = await fixture_loop(database, kind)
    await loop._tick(config)
    event, validate, handler = ports.events[0]
    entered, finished = asyncio.Event(), asyncio.Event()

    async def background() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finished.set()

    task = asyncio.create_task(background())
    loop._background.add(task)
    task.add_done_callback(loop._background.discard)
    await entered.wait()
    await loop.stop()
    assert finished.is_set() and task.done() and not loop._background
    assert not await validate()
    await handler(event, SimpleNamespace(decision=SimpleNamespace(decision="inform")))
    assert not ports.deliveries
