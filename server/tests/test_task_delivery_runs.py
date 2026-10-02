import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, Base, Database, TaskRunRecord, create_database
from app.ids import uuid7
from app.runs.delivery import outcome
from app.tasks import RepeatKind, TaskKind, TaskScheduler, TaskStatus, TaskStore, TaskTrigger


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    value = create_database(f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


async def seed(database: Database) -> tuple[UUID, TaskStore]:
    owner = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=owner, display_name="Fixture", status="active"))
    return owner, TaskStore(database)


async def test_concurrent_task_deliveries_create_one_run(database: Database) -> None:
    owner, store = await seed(database)
    now = datetime.now(UTC)
    task = await store.create(
        user_id=owner,
        kind=TaskKind.REMINDER,
        title="private fixture title",
        trigger=TaskTrigger(type="time", at=now + timedelta(seconds=1)),
        now=now,
    )
    calls = 0

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return ["web_chat"]

    schedulers = [TaskScheduler(store, deliverer=dispatch) for _ in range(8)]
    await asyncio.gather(
        *[scheduler.run_once(now=now + timedelta(seconds=2)) for scheduler in schedulers]
    )
    assert calls == 1
    view = await store.get_task(owner, task.id)
    assert view.status == TaskStatus.DONE and view.last_delivery is not None
    run_id = UUID(str(view.last_delivery["run_id"]))
    async with database.sessions() as session:
        roots = list(
            await session.scalars(select(TaskRunRecord).where(TaskRunRecord.user_id == owner))
        )
        assert len(roots) == 1 and roots[0].id == run_id and roots[0].status == "succeeded"
        assert roots[0].contract["resource_usage"]["tool_attempts"] == 1
        assert "private fixture title" not in str(roots[0].contract)


async def test_recurring_firings_have_separate_runs_and_optout_is_explicit(
    database: Database,
) -> None:
    owner, store = await seed(database)
    now = datetime.now(UTC)
    task = await store.create(
        user_id=owner,
        kind=TaskKind.REMINDER,
        title="Fixture",
        trigger=TaskTrigger(
            type="time", at=now + timedelta(seconds=1), repeat_kind=RepeatKind.DAILY
        ),
        now=now,
    )

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        return ["web_chat"]

    scheduler = TaskScheduler(
        store, deliverer=dispatch, budget_loader=lambda: RunBudgetConfig(enabled=False)
    )
    await scheduler.run_once(now=now + timedelta(seconds=2))
    first = await store.get_task(owner, task.id)
    await scheduler.run_once(now=now + timedelta(days=1, seconds=2))
    second = await store.get_task(owner, task.id)
    assert first.last_delivery and second.last_delivery
    assert first.last_delivery["run_id"] != second.last_delivery["run_id"]
    assert second.fire_count == 2 and second.status == TaskStatus.ACTIVE
    async with database.sessions() as session:
        roots = list(
            await session.scalars(select(TaskRunRecord).where(TaskRunRecord.user_id == owner))
        )
        assert len(roots) == 2
        assert all(root.budget and root.budget["enabled"] is False for root in roots)
        assert all("resource_usage" not in root.contract for root in roots)


async def test_cancel_firing_reminder_stops_transport_and_preserves_cancelled_source(
    database: Database,
) -> None:
    owner, store = await seed(database)
    now = datetime.now(UTC)
    task = await store.create(
        user_id=owner,
        kind=TaskKind.REMINDER,
        title="Fixture",
        trigger=TaskTrigger(type="time", at=now + timedelta(seconds=1)),
        now=now,
    )
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        started.set()
        try:
            await asyncio.sleep(20)
        finally:
            cancelled.set()
        return ["web_chat"]

    scheduler = TaskScheduler(store, deliverer=dispatch)
    firing = asyncio.create_task(scheduler.run_once(now=now + timedelta(seconds=2)))
    await asyncio.wait_for(started.wait(), 3)
    # A second instance's recovery must leave this live Run alone.
    assert await store.recover_interrupted(now=datetime.now(UTC)) == 0
    await store.cancel_task(owner, task.id)
    await asyncio.wait_for(firing, 3)
    assert cancelled.is_set()
    view = await store.get_task(owner, task.id)
    assert view.status == TaskStatus.CANCELLED and view.last_delivery
    async with database.sessions() as session:
        root = await session.get(TaskRunRecord, UUID(str(view.last_delivery["run_id"])))
        assert root is not None and root.status == "cancelled"
        result = outcome(root)
        assert result is not None and result.status == "unknown"
    assert await scheduler.run_once() == 0


async def test_late_previous_firing_cannot_overwrite_next_firing(database: Database) -> None:
    owner, store = await seed(database)
    now = datetime.now(UTC)
    task = await store.create(
        user_id=owner,
        kind=TaskKind.REMINDER,
        title="Fixture",
        trigger=TaskTrigger(type="event", event_type="fixture.event", cooldown_seconds=60),
        now=now,
    )
    first = (await store.claim_event(owner, "fixture.event", now=now))[0]
    started = asyncio.Event()

    async def slow(text: str, **kwargs: object) -> list[str]:
        started.set()
        await asyncio.sleep(20)
        return ["desktop"]

    old = asyncio.create_task(
        TaskScheduler(store, deliverer=slow)._fire(first, trigger_kind="task.event")
    )
    await asyncio.wait_for(started.wait(), 3)
    second = (await store.claim_event(owner, "fixture.event", now=now + timedelta(seconds=61)))[0]

    async def fast(text: str, **kwargs: object) -> list[str]:
        return ["web_chat"]

    await TaskScheduler(store, deliverer=fast)._fire(second, trigger_kind="task.event")
    await asyncio.wait_for(old, 3)
    view = await store.get_task(owner, task.id)
    assert view.last_delivery and view.last_delivery["run_id"] == str(second.run_id)
    assert view.last_delivery["channels"] == ["web_chat"]
    async with database.sessions() as session:
        root = await session.get(TaskRunRecord, first.run_id)
        assert root is not None and root.status == "failed"
        assert root.contract["dispatch_state"] == "unknown"


async def test_inactive_owner_is_not_claimed(database: Database) -> None:
    owner, store = await seed(database)
    now = datetime.now(UTC)
    await store.create(
        user_id=owner,
        kind=TaskKind.REMINDER,
        title="Fixture",
        trigger=TaskTrigger(type="time", at=now + timedelta(seconds=1)),
        now=now,
    )
    async with database.sessions.begin() as session:
        user = await session.get(AppUserRecord, owner)
        assert user is not None
        user.status = "disabled"
    assert await store.claim_due(now=now + timedelta(seconds=2)) == []


async def test_scheduler_stop_cancels_owned_inflight_task(database: Database) -> None:
    owner, store = await seed(database)
    now = datetime.now(UTC)
    task = await store.create(
        user_id=owner,
        kind=TaskKind.REMINDER,
        title="Fixture",
        trigger=TaskTrigger(type="time", at=now + timedelta(seconds=1)),
        now=now,
    )
    started = asyncio.Event()

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        started.set()
        await asyncio.sleep(20)
        return ["web_chat"]

    scheduler = TaskScheduler(
        store, deliverer=dispatch, interval_seconds=0.01, clock=lambda: now + timedelta(seconds=2)
    )
    scheduler.start()
    await asyncio.wait_for(started.wait(), 3)
    await asyncio.wait_for(scheduler.stop(), 3)
    view = await store.get_task(owner, task.id)
    assert view.status == TaskStatus.DONE and view.last_delivery
    async with database.sessions() as session:
        root = await session.get(TaskRunRecord, UUID(str(view.last_delivery["run_id"])))
        assert root is not None and root.status == "cancelled"
        assert root.contract["dispatch_state"] == "unknown"


@pytest.mark.parametrize("action", ["complete", "snooze", "cancel_source"])
async def test_source_controls_cancel_current_run_in_same_transaction(
    database: Database, action: str
) -> None:
    owner, store = await seed(database)
    now = datetime.now(UTC)
    task = await store.create(
        user_id=owner,
        kind=TaskKind.REMINDER,
        title="Fixture",
        source_ref="fixture-source",
        trigger=TaskTrigger(type="time", at=now + timedelta(seconds=1)),
        now=now,
    )
    started = asyncio.Event()

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        started.set()
        await asyncio.sleep(20)
        return ["web_chat"]

    scheduler = TaskScheduler(store, deliverer=dispatch)
    firing = asyncio.create_task(scheduler.run_once(now=now + timedelta(seconds=2)))
    await asyncio.wait_for(started.wait(), 3)
    if action == "complete":
        await store.complete_task(owner, task.id)
    elif action == "snooze":
        await store.snooze_task(owner, task.id, until=now + timedelta(hours=1))
    else:
        assert await store.cancel_tasks_by_source_ref(owner, "fixture-source") == 1
    view = await store.get_task(owner, task.id)
    assert view.last_delivery
    async with database.sessions() as session:
        root = await session.get(TaskRunRecord, UUID(str(view.last_delivery["run_id"])))
        assert root is not None and root.status == "cancelled"
    await asyncio.wait_for(firing, 3)
    updated = await store.get_task(owner, task.id)
    assert updated.last_delivery and updated.last_delivery["outcome"] == "unknown"
    if action == "snooze":
        assert updated.status == TaskStatus.ACTIVE and updated.next_fire_at == now + timedelta(
            hours=1
        )


async def test_long_dispatch_does_not_preclaim_rest_of_due_batch(database: Database) -> None:
    owner, store = await seed(database)
    now = datetime.now(UTC)
    values = [
        await store.create(
            user_id=owner,
            kind=TaskKind.REMINDER,
            title=f"Fixture {index}",
            trigger=TaskTrigger(type="time", at=now + timedelta(seconds=index + 1)),
            now=now,
        )
        for index in range(2)
    ]
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        return ["web_chat"]

    scheduler = TaskScheduler(store, deliverer=dispatch)
    firing = asyncio.create_task(scheduler.run_once(now=now + timedelta(seconds=3)))
    await asyncio.wait_for(started.wait(), 3)
    pending = await store.get_task(owner, values[1].id)
    assert (
        pending.status == TaskStatus.ACTIVE
        and pending.fire_count == 0
        and pending.last_delivery is None
    )
    assert await store.recover_interrupted(now=now + timedelta(seconds=3)) == 0
    release.set()
    assert await asyncio.wait_for(firing, 3) == 2
    assert calls == 2
