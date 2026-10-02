import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from app.cognition import CognitiveStore, GoalKind, GoalStatus
from app.config.models import RunBudgetConfig
from app.db import (
    AppUserRecord,
    Base,
    CognitiveGoalRecord,
    Database,
    TaskRunRecord,
    create_database,
)
from app.ids import uuid7
from app.runs.delivery import outcome
from app.tasks.goal_scheduler import GoalReminderScheduler


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    value = create_database(f"sqlite+aiosqlite:///{tmp_path / 'goals.db'}")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


async def seed(database: Database) -> tuple[UUID, CognitiveStore, UUID]:
    owner = uuid7()
    async with database.sessions.begin() as session:
        session.add(
            AppUserRecord(
                id=owner, display_name="Fixture", status="active", timezone="Asia/Shanghai"
            )
        )
    store = CognitiveStore(database)
    goal = await store.create_goal(
        user_id=owner,
        kind=GoalKind.USER,
        title="Fixture",
        source_kind="manual",
        source_id="fixture",
        due_at=datetime.now(UTC) + timedelta(hours=2),
    )
    return owner, store, goal.id


async def roots(database: Database) -> list[TaskRunRecord]:
    async with database.sessions() as session:
        return list(await session.scalars(select(TaskRunRecord).order_by(TaskRunRecord.created_at)))


async def test_duplicate_claim_has_one_dispatch_and_budget(database: Database) -> None:
    _, store, _ = await seed(database)
    item = (await store.claim_due_goal_reminders())[0]
    calls = 0

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return ["web_chat"]

    schedulers = [GoalReminderScheduler(store, deliverer=dispatch) for _ in range(8)]
    await asyncio.gather(*(scheduler._remind(item) for scheduler in schedulers))
    rows = await roots(database)
    assert calls == len(rows) == 1
    assert rows[0].status == "succeeded"
    assert rows[0].contract["source_phase"] == "pre_due"
    assert rows[0].contract["source_timezone"] == "Asia/Shanghai"
    assert rows[0].contract["resource_usage"]["tool_attempts"] == 1
    result = outcome(rows[0])
    assert result and result.status == "returned" and result.validation_level == "V1"


async def test_phases_are_separate_and_goal_stays_active(database: Database) -> None:
    owner, store, identifier = await seed(database)
    calls: list[str] = []

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        calls.append(text)
        return ["desktop"]

    scheduler = GoalReminderScheduler(
        store, deliverer=dispatch, budget_loader=lambda: RunBudgetConfig(enabled=False)
    )
    now = datetime.now(UTC)
    assert await scheduler.run_once(now=now) == 1
    assert await scheduler.run_once(now=now + timedelta(hours=3)) == 1
    rows = await roots(database)
    assert len(rows) == 2 and rows[0].request_id != rows[1].request_id
    assert all(row.budget and row.budget["enabled"] is False for row in rows)
    assert all("resource_usage" not in row.contract for row in rows)
    async with database.sessions() as session:
        goal = await session.get(CognitiveGoalRecord, identifier)
        assert goal and goal.user_id == owner and goal.status == "active"
        assert goal.due_at
        expected = goal.due_at.replace(tzinfo=UTC) + timedelta(hours=8)
        assert expected.strftime("%m-%d %H:%M") in calls[0]


@pytest.mark.parametrize(
    "control", ["cancel", "complete", "defer", "ignore", "shutdown", "private"]
)
async def test_controls_stop_inflight_and_preserve_unknown(
    database: Database, control: str
) -> None:
    owner, store, identifier = await seed(database)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()
        return ["web_chat"]

    scheduler = GoalReminderScheduler(store, deliverer=dispatch)
    firing = asyncio.create_task(scheduler.run_once())
    await asyncio.wait_for(started.wait(), 3)
    if control in {"cancel", "complete"}:
        await store.set_goal_status(
            user_id=owner,
            goal_id=identifier,
            status=GoalStatus.CANCELLED if control == "cancel" else GoalStatus.COMPLETED,
        )
    elif control == "defer":
        await store.defer_goal_reminders(
            owner, identifier, until=datetime.now(UTC) + timedelta(days=1)
        )
    elif control == "ignore":
        await store.ignore_goal_reminder(owner, identifier)
    elif control == "shutdown":
        await asyncio.wait_for(scheduler.stop(), 3)
    else:
        async with database.sessions.begin() as session:
            goal = await session.get(CognitiveGoalRecord, identifier)
            assert goal
            goal.privacy_level = "L2"
    await asyncio.wait_for(asyncio.gather(firing, return_exceptions=True), 3)
    assert stopped.is_set()
    row = (await roots(database))[0]
    assert row.status == ("failed" if control == "private" else "cancelled")
    result = outcome(row)
    assert result and result.status == "unknown"
    assert await scheduler.run_once() == 0


async def test_failed_transport_and_no_deliverer_do_not_replay(database: Database) -> None:
    _, store, _ = await seed(database)
    calls = 0

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        nonlocal calls
        calls += 1
        raise RuntimeError("secret fixture body")

    scheduler = GoalReminderScheduler(store, deliverer=dispatch)
    assert await scheduler.run_once() == 1
    assert await scheduler.run_once() == 0
    row = (await roots(database))[0]
    assert calls == 1 and row.contract["delivery_reason"] == "RuntimeError"
    assert row.contract["dispatch_state"] == "unknown"
    _, other, _ = await seed(database)
    assert await GoalReminderScheduler(other).run_once() == 1
    row = (await roots(database))[-1]
    assert row.contract["delivery_reason"] == "no_deliverer"
    assert row.contract["dispatch_state"] == "not_started"
    assert "resource_usage" not in row.contract


async def test_eligible_goal_not_starved_by_claimed_deferred_or_expired(database: Database) -> None:
    owner, store, _ = await seed(database)
    now = datetime.now(UTC)
    await store.claim_due_goal_reminders(now=now)
    for index in range(25):
        goal = await store.create_goal(
            user_id=owner,
            kind=GoalKind.USER,
            title=f"Blocked {index}",
            source_kind="manual",
            source_id=f"blocked:{index}",
            due_at=now - timedelta(days=1),
        )
        if index % 2:
            await store.defer_goal_reminders(owner, goal.id, until=now + timedelta(days=1))
        else:
            async with database.sessions.begin() as session:
                record = await session.get(CognitiveGoalRecord, goal.id)
                assert record
                record.expires_at = now - timedelta(hours=1)
    eligible = await store.create_goal(
        user_id=owner,
        kind=GoalKind.USER,
        title="Eligible",
        source_kind="manual",
        source_id="eligible",
        due_at=now + timedelta(hours=4),
    )
    claimed = await store.claim_due_goal_reminders(now=now, limit=1)
    assert len(claimed) == 1 and claimed[0].goal.id == eligible.id


async def test_timeout_retains_unknown_and_same_claim_cannot_retry(database: Database) -> None:
    _, store, _ = await seed(database)
    item = (await store.claim_due_goal_reminders())[0]
    calls = 0

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(30)
        return ["web_chat"]

    scheduler = GoalReminderScheduler(
        store,
        deliverer=dispatch,
        budget_loader=lambda: RunBudgetConfig(maintenance_deadline_seconds=1),
    )
    await asyncio.wait_for(scheduler._remind(item), 3)
    await scheduler._remind(item)
    rows = await roots(database)
    assert calls == len(rows) == 1
    assert rows[0].status == "failed" and rows[0].contract["dispatch_state"] == "unknown"
    assert rows[0].contract["resource_usage"]["tool_attempts"] == 1
