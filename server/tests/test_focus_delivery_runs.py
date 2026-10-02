import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from test_goal_delivery_runs import database as goal_database
from test_goal_delivery_runs import seed

from app.config.models import RunBudgetConfig
from app.db import Database, TaskRunRecord, TimelineEventRecord
from app.focus import FocusScheduler, FocusService
from app.ids import uuid7
from app.runs.delivery import outcome
from app.runs.store import RunStore
from app.schemas import PrivacyLevel
from app.timeline import TimelineStore

database = goal_database


async def focus(database: Database) -> tuple[UUID, FocusService, dict[str, datetime]]:
    owner, _, _ = await seed(database)
    clock = {"now": datetime.now(UTC)}
    timeline = TimelineStore(database)
    service = FocusService(timeline, clock=lambda: clock["now"])
    service.start_session(str(owner), target="coding", keywords=["coding"], duration_minutes=120)
    for age in (100, 50, 5):
        await timeline.index_screen_observation(
            user_id=owner,
            observation_id=uuid7(),
            display=0,
            summary="coding backend fixture",
            privacy_level=PrivacyLevel.L1,
            occurred_at=clock["now"] - timedelta(minutes=age),
        )
    return owner, service, clock


async def runs(database: Database, owner: UUID) -> list[TaskRunRecord]:
    async with database.sessions() as session:
        return list(
            await session.scalars(
                select(TaskRunRecord)
                .where(TaskRunRecord.user_id == owner)
                .order_by(TaskRunRecord.created_at)
            )
        )


async def test_same_focus_cycle_has_one_dispatch_across_schedulers(database: Database) -> None:
    owner, service, clock = await focus(database)
    calls = 0

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return ["web_chat"]

    schedulers = [
        FocusScheduler(service, clock=lambda: clock["now"], deliverer=dispatch) for _ in range(8)
    ]
    await asyncio.gather(*(scheduler.run_once() for scheduler in schedulers))
    records = await runs(database, owner)
    assert calls == len(records) == 1
    assert records[0].status == "succeeded" and records[0].contract["source_generation"] == 0
    assert records[0].contract["resource_usage"]["tool_attempts"] == 1
    view = await RunStore(database).get(records[0].id, user_id=owner)
    assert view.goal and view.goal.status == "passed"
    assert service.get_session(str(owner)) is not None


async def test_next_cooled_cycle_is_separate_and_optout_retains_guard(database: Database) -> None:
    owner, service, clock = await focus(database)
    calls = 0

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        nonlocal calls
        calls += 1
        return ["desktop"]

    scheduler = FocusScheduler(
        service,
        clock=lambda: clock["now"],
        deliverer=dispatch,
        budget_loader=lambda: RunBudgetConfig(enabled=False),
    )
    assert await scheduler.run_once() == 1
    assert await scheduler.run_once() == 0
    clock["now"] += timedelta(minutes=25)
    assert await scheduler.run_once() == 1
    records = await runs(database, owner)
    assert calls == len(records) == 2 and records[0].id != records[1].id
    assert [record.contract["source_generation"] for record in records] == [0, 1]
    assert all(record.budget and record.budget["enabled"] is False for record in records)
    assert all("resource_usage" not in record.contract for record in records)


@pytest.mark.parametrize("control", ["stop", "replace", "shutdown", "private", "delete"])
async def test_focus_controls_and_evidence_revoke_inflight_dispatch(
    database: Database, control: str
) -> None:
    owner, service, clock = await focus(database)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()
        return ["web_chat"]

    scheduler = FocusScheduler(service, clock=lambda: clock["now"], deliverer=dispatch)
    running = asyncio.create_task(scheduler.run_once())
    await asyncio.wait_for(started.wait(), 3)
    if control == "stop":
        service.stop_session(str(owner))
    elif control == "replace":
        service.start_session(
            str(owner), target="replacement", keywords=["replacement"], duration_minutes=120
        )
    elif control == "shutdown":
        await asyncio.wait_for(scheduler.stop(), 3)
    else:
        async with database.sessions.begin() as session:
            entry = await session.scalar(
                select(TimelineEventRecord).where(TimelineEventRecord.user_id == owner)
            )
            assert entry
            if control == "private":
                entry.privacy_level = "L2"
            else:
                await session.delete(entry)
    await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 3)
    assert stopped.is_set()
    record = (await runs(database, owner))[0]
    assert record.status == ("failed" if control in {"private", "delete"} else "cancelled")
    result = outcome(record)
    assert result and result.status == "unknown"
    if control == "replace":
        current = service.get_session(str(owner))
        assert current and not current.nudged_at


async def test_unknown_focus_attempt_does_not_retry_in_same_cycle(database: Database) -> None:
    owner, service, clock = await focus(database)
    calls = 0

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        nonlocal calls
        calls += 1
        raise RuntimeError("private transport fixture")

    scheduler = FocusScheduler(service, clock=lambda: clock["now"], deliverer=dispatch)
    assert await scheduler.run_once() == 1
    assert await scheduler.run_once() == 0
    record = (await runs(database, owner))[0]
    assert calls == 1 and record.status == "failed"
    assert record.contract["dispatch_state"] == "unknown"
    assert record.contract["delivery_reason"] == "RuntimeError"


async def test_focus_absolute_deadline_stops_blocked_transport(database: Database) -> None:
    from sqlalchemy import update

    owner, service, clock = await focus(database)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()
        return ["web_chat"]

    scheduler = FocusScheduler(service, clock=lambda: clock["now"], deliverer=dispatch)
    running = asyncio.create_task(scheduler.run_once())
    await asyncio.wait_for(started.wait(), 3)
    async with database.sessions.begin() as session:
        await session.execute(
            update(TaskRunRecord)
            .where(TaskRunRecord.user_id == owner)
            .values(deadline=datetime.now(UTC) - timedelta(seconds=1))
        )
    await asyncio.wait_for(running, 3)
    assert stopped.is_set()
    record = (await runs(database, owner))[0]
    assert record.contract["delivery_reason"] == "run_deadline_exceeded"
    assert record.contract["dispatch_state"] == "unknown"
    assert await scheduler.run_once() == 0


async def test_inactive_focus_owner_is_skipped_before_evaluation(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import AppUserRecord

    owner, service, clock = await focus(database)
    async with database.sessions.begin() as session:
        user = await session.get(AppUserRecord, owner)
        assert user
        user.status = "inactive"

    async def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("inactive focus owner must not be evaluated or delivered")

    monkeypatch.setattr(service, "evaluate_snapshot", forbidden)
    assert (
        await FocusScheduler(service, clock=lambda: clock["now"], deliverer=forbidden).run_once()
        == 0
    )
    assert not await runs(database, owner)
