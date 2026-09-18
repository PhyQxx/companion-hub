from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import create_tasks_router
from app.auth import AuthService
from app.db import AppUserRecord, Base, Database, TaskItemRecord, create_database
from app.ids import uuid7
from app.schemas.common import PrivacyLevel
from app.tasks import (
    ClaimedTask,
    RepeatKind,
    TaskKind,
    TaskScheduler,
    TaskStatus,
    TaskStore,
    TaskTrigger,
    compute_next_fire,
)


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
async def user_id(database: Database) -> UUID:
    value = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=value, display_name="Task owner", status="active"))
    return value


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# compute_next_fire 纯函数
# ---------------------------------------------------------------------------


def _trigger(**kwargs: object) -> TaskTrigger:
    return TaskTrigger(type="time", at=NOW, **kwargs)


class FakeSemanticEvent:
    """满足 TaskScheduler.on_semantic_event 协议的最小事件。"""

    def __init__(self, user_id: UUID, kind: str) -> None:
        self.user_id = user_id
        self.kind = kind


def test_once_has_no_next_fire() -> None:
    assert compute_next_fire(_trigger(), after=NOW) is None


def test_daily_advances_one_day_and_keeps_wall_time() -> None:
    nxt = compute_next_fire(_trigger(repeat_kind=RepeatKind.DAILY), after=NOW + timedelta(hours=1))
    assert nxt == NOW + timedelta(days=1)


def test_daily_skips_missed_occurrences() -> None:
    after = NOW + timedelta(days=3, hours=2)
    nxt = compute_next_fire(_trigger(repeat_kind=RepeatKind.DAILY), after=after)
    assert nxt == NOW + timedelta(days=4)


def test_weekdays_skips_weekend() -> None:
    # 2026-09-01 是周二；锚点挪到周五 18:00，下一次是下周一同一时间
    friday = datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    nxt = compute_next_fire(
        TaskTrigger(type="time", at=friday, repeat_kind=RepeatKind.WEEKDAYS),
        after=friday + timedelta(hours=1),
    )
    assert nxt == datetime(2026, 9, 7, 18, 0, tzinfo=UTC)


def test_weekly_matches_declared_weekdays() -> None:
    nxt = compute_next_fire(
        _trigger(repeat_kind=RepeatKind.WEEKLY, weekdays=[0, 2]),
        after=NOW + timedelta(minutes=1),  # 周二 12:01 已过 → 下一个是周三 12:00
    )
    assert nxt == datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def test_interval_rounds_up_to_next_period() -> None:
    nxt = compute_next_fire(
        _trigger(repeat_kind=RepeatKind.INTERVAL, interval_minutes=30),
        after=NOW + timedelta(minutes=45),
    )
    assert nxt == NOW + timedelta(minutes=60)


# ---------------------------------------------------------------------------
# TaskStore
# ---------------------------------------------------------------------------


async def test_create_oneshot_reminder_sets_next_fire(database: Database, user_id: UUID) -> None:
    store = TaskStore(database)
    task = await store.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="晚上 8 点提醒我吃药",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(hours=8)),
        now=NOW,
    )
    assert task.status == TaskStatus.ACTIVE
    assert task.next_fire_at == NOW + timedelta(hours=8)
    assert task.trigger.repeat_kind == RepeatKind.ONCE


async def test_create_rejects_invalid_triggers(database: Database, user_id: UUID) -> None:
    store = TaskStore(database)
    with pytest.raises(ValueError, match="未来"):
        await store.create(
            user_id=user_id,
            kind=TaskKind.REMINDER,
            title="过期",
            trigger=TaskTrigger(type="time", at=NOW - timedelta(minutes=1)),
            now=NOW,
        )
    with pytest.raises(ValueError, match="event_type"):
        await store.create(
            user_id=user_id,
            kind=TaskKind.REMINDER,
            title="无事件",
            trigger=TaskTrigger(type="event"),
            now=NOW,
        )
    with pytest.raises(ValueError, match="interval"):
        await store.create(
            user_id=user_id,
            kind=TaskKind.REMINDER,
            title="缺间隔",
            trigger=TaskTrigger(
                type="time", at=NOW + timedelta(hours=1), repeat_kind=RepeatKind.INTERVAL
            ),
            now=NOW,
        )
    with pytest.raises(ValueError, match="weekdays"):
        await store.create(
            user_id=user_id,
            kind=TaskKind.REMINDER,
            title="缺星期",
            trigger=TaskTrigger(
                type="time", at=NOW + timedelta(hours=1), repeat_kind=RepeatKind.WEEKLY
            ),
            now=NOW,
        )


async def test_complete_cancel_snooze_guards(database: Database, user_id: UUID) -> None:
    store = TaskStore(database)
    other = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=other, display_name="Other", status="active"))

    task = await store.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="稍后验证",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(hours=1)),
        now=NOW,
    )
    with pytest.raises(LookupError):
        await store.get_task(other, task.id)

    snoozed = await store.snooze_task(user_id, task.id, until=NOW + timedelta(hours=2), now=NOW)
    assert snoozed.next_fire_at == NOW + timedelta(hours=2)
    with pytest.raises(ValueError, match="未来"):
        await store.snooze_task(user_id, task.id, until=NOW - timedelta(minutes=1), now=NOW)

    done = await store.complete_task(user_id, task.id, now=NOW)
    assert done.status == TaskStatus.DONE
    assert done.completed_at is not None
    with pytest.raises(ValueError, match="不能取消"):
        await store.cancel_task(user_id, task.id, now=NOW)


async def test_claim_due_oneshot_exactly_once(database: Database, user_id: UUID) -> None:
    store = TaskStore(database)
    task = await store.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="只提醒一次",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(minutes=1)),
        now=NOW,
    )
    first = await store.claim_due(now=NOW + timedelta(minutes=2))
    second = await store.claim_due(now=NOW + timedelta(minutes=3))
    assert [item.id for item in first] == [task.id]
    assert first[0].oneshot is True
    assert second == []
    firing = await store.get_task(user_id, task.id)
    assert firing.status == TaskStatus.FIRING
    await store.finish_firing(task.id, delivery={"channels": ["web_chat"]})
    finished = await store.get_task(user_id, task.id)
    assert finished.status == TaskStatus.DONE
    assert finished.fire_count == 1
    assert finished.last_delivery == {"channels": ["web_chat"]}
    # 已完成后不再触发
    assert await store.claim_due(now=NOW + timedelta(minutes=10)) == []


async def test_claim_due_recurring_advances_and_skips_catchup(
    database: Database, user_id: UUID
) -> None:
    store = TaskStore(database)
    task = await store.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="每天提醒",
        trigger=TaskTrigger(type="time", at=NOW, repeat_kind=RepeatKind.DAILY),
        now=NOW,
    )
    # 服务器停机 3 天后恢复：只触发一次，下一次是明天，不补发错过日期
    claimed = await store.claim_due(now=NOW + timedelta(days=3))
    assert [item.id for item in claimed] == [task.id]
    assert claimed[0].oneshot is False
    view = await store.get_task(user_id, task.id)
    assert view.status == TaskStatus.ACTIVE
    assert view.fire_count == 1
    assert view.next_fire_at == NOW + timedelta(days=4)


async def test_claim_event_respects_cooldown_and_matching(
    database: Database, user_id: UUID
) -> None:
    store = TaskStore(database)
    arrive = await store.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="到家提醒拿快递",
        trigger=TaskTrigger(type="event", event_type="user_arrived_home", cooldown_seconds=120),
        now=NOW,
    )
    leave = await store.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="离家关空调",
        trigger=TaskTrigger(type="event", event_type="user_left_home"),
        now=NOW,
    )
    fired = await store.claim_event(user_id, "user_arrived_home", now=NOW)
    assert [item.id for item in fired] == [arrive.id]
    # 默认 cooldown 生效：120 秒内同事件不再触发
    again = await store.claim_event(user_id, "user_arrived_home", now=NOW + timedelta(seconds=60))
    assert again == []
    later = await store.claim_event(user_id, "user_arrived_home", now=NOW + timedelta(seconds=180))
    assert [item.id for item in later] == [arrive.id]
    # 事件触发任务不进入时间到期认领
    assert await store.claim_due(now=NOW + timedelta(days=30)) == []
    # 另一个用户的任务不会被认领
    other = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=other, display_name="Other", status="active"))
    assert await store.claim_event(other, "user_left_home", now=NOW) == []
    assert leave.status == TaskStatus.ACTIVE


async def test_recover_interrupted_marks_done_without_refire(
    database: Database, user_id: UUID
) -> None:
    store = TaskStore(database)
    task = await store.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="中断恢复",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(minutes=1)),
        now=NOW,
    )
    claimed = await store.claim_due(now=NOW + timedelta(minutes=2))
    assert len(claimed) == 1
    # 模拟进程在投递中途崩溃：遗留 firing，重启后直接判完成且不重复投递
    assert await store.claim_due(now=NOW + timedelta(minutes=3)) == []
    recovered = await store.recover_interrupted(now=NOW + timedelta(minutes=4))
    assert recovered == 1
    view = await store.get_task(user_id, task.id)
    assert view.status == TaskStatus.DONE
    assert view.last_delivery == {"reason_code": "interrupted"}
    assert await store.claim_due(now=NOW + timedelta(minutes=5)) == []


async def test_cancelled_task_never_fires(database: Database, user_id: UUID) -> None:
    store = TaskStore(database)
    task = await store.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="取消后不触发",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(minutes=5)),
        now=NOW,
    )
    await store.cancel_task(user_id, task.id)
    assert await store.claim_due(now=NOW + timedelta(minutes=10)) == []


# ---------------------------------------------------------------------------
# TaskScheduler
# ---------------------------------------------------------------------------


class RecordingDeliverer:
    def __init__(self, channels: list[str] | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.channels = channels if channels is not None else ["web_chat"]
        self.error = error

    async def __call__(
        self,
        text: str,
        *,
        user_id: UUID,
        task_id: UUID,
        privacy_level: PrivacyLevel,
        trigger_kind: str,
    ) -> list[str] | None:
        self.calls.append(
            {
                "text": text,
                "user_id": user_id,
                "task_id": task_id,
                "privacy_level": privacy_level,
                "trigger_kind": trigger_kind,
            }
        )
        if self.error is not None:
            raise self.error
        return self.channels


async def test_scheduler_fires_oneshot_and_records_delivery(
    database: Database, user_id: UUID
) -> None:
    store = TaskStore(database)
    task = await store.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="提醒我喝水",
        notes="楼下接水",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(minutes=1)),
        now=NOW,
    )
    deliverer = RecordingDeliverer()
    scheduler = TaskScheduler(store, deliverer=deliverer)
    fired = await scheduler.run_once(now=NOW + timedelta(minutes=2))
    assert fired == 1
    assert len(deliverer.calls) == 1
    assert deliverer.calls[0]["task_id"] == task.id
    assert "提醒我喝水" in str(deliverer.calls[0]["text"])
    view = await store.get_task(user_id, task.id)
    assert view.status == TaskStatus.DONE
    assert view.last_delivery is not None
    assert view.last_delivery["channels"] == ["web_chat"]
    # 再跑一次不会重复触发
    assert await scheduler.run_once(now=NOW + timedelta(minutes=3)) == 0


async def test_scheduler_recurring_keeps_active(database: Database, user_id: UUID) -> None:
    store = TaskStore(database)
    task = await store.create(
        user_id=user_id,
        kind=TaskKind.TASK,
        title="每天站会",
        trigger=TaskTrigger(type="time", at=NOW, repeat_kind=RepeatKind.DAILY),
        now=NOW,
    )
    scheduler = TaskScheduler(store, deliverer=RecordingDeliverer())
    assert await scheduler.run_once(now=NOW + timedelta(days=1)) == 1
    view = await store.get_task(user_id, task.id)
    assert view.status == TaskStatus.ACTIVE
    assert view.next_fire_at == NOW + timedelta(days=2)


async def test_scheduler_without_deliverer_still_completes(
    database: Database, user_id: UUID
) -> None:
    store = TaskStore(database)
    task = await store.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="无投递通道",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(minutes=1)),
        now=NOW,
    )
    scheduler = TaskScheduler(store)
    assert await scheduler.run_once(now=NOW + timedelta(minutes=2)) == 1
    view = await store.get_task(user_id, task.id)
    assert view.status == TaskStatus.DONE
    assert view.last_delivery is not None
    assert view.last_delivery["reason_code"] == "no_deliverer"


async def test_scheduler_deliverer_error_does_not_retrigger(
    database: Database, user_id: UUID
) -> None:
    store = TaskStore(database)
    task = await store.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="投递异常",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(minutes=1)),
        now=NOW,
    )
    scheduler = TaskScheduler(store, deliverer=RecordingDeliverer(error=RuntimeError("boom")))
    assert await scheduler.run_once(now=NOW + timedelta(minutes=2)) == 1
    view = await store.get_task(user_id, task.id)
    assert view.status == TaskStatus.DONE
    assert view.last_delivery is not None
    assert str(view.last_delivery["reason_code"]).startswith("deliver_error")
    assert await scheduler.run_once(now=NOW + timedelta(minutes=3)) == 0


async def test_scheduler_event_trigger_via_observer(database: Database, user_id: UUID) -> None:
    store = TaskStore(database)
    task = await store.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="到家提醒",
        trigger=TaskTrigger(type="event", event_type="user_arrived_home"),
        now=NOW,
    )
    deliverer = RecordingDeliverer()
    scheduler = TaskScheduler(store, deliverer=deliverer)

    await scheduler.on_semantic_event(FakeSemanticEvent(user_id, "user_arrived_home"))
    assert len(deliverer.calls) == 1
    view = await store.get_task(user_id, task.id)
    assert view.status == TaskStatus.ACTIVE  # 事件触发默认 cooldown，任务保持 active

    # observer 异常不外溢
    async def broken_deliverer(*args: object, **kwargs: object) -> list[str]:
        raise RuntimeError("boom")

    scheduler.set_deliverer(broken_deliverer)
    await scheduler.on_semantic_event(FakeSemanticEvent(user_id, "user_left_home"))  # 不应抛出


async def test_scheduler_loop_start_stop_roundtrip(database: Database, user_id: UUID) -> None:
    store = TaskStore(database)
    await store.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="循环内触发",
        trigger=TaskTrigger(type="time", at=datetime.now(UTC) + timedelta(milliseconds=10)),
    )
    deliverer = RecordingDeliverer()
    scheduler = TaskScheduler(store, interval_seconds=0.05, deliverer=deliverer)
    scheduler.start()
    for _ in range(50):
        if deliverer.calls:
            break
        await asyncio.sleep(0.02)
    await scheduler.stop()
    assert len(deliverer.calls) == 1


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


async def test_tasks_api_lifecycle(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="Task user", password="correct horse")
    store = TaskStore(database)
    app = FastAPI()
    app.include_router(create_tasks_router(store, auth))
    headers = {"Authorization": f"Bearer {owner.access_token}"}
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/api/v1/tasks")
        created = await client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "kind": "reminder",
                "title": "明天上午提醒我开会",
                "trigger": {"type": "time", "at": future},
            },
        )
        invalid = await client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "title": "过去时间",
                "trigger": {"type": "time", "at": "2020-01-01T00:00:00Z"},
            },
        )
        task_id = created.json()["id"]
        listed = await client.get("/api/v1/tasks?status=active", headers=headers)
        patched = await client.patch(
            f"/api/v1/tasks/{task_id}",
            headers=headers,
            json={"priority": 2},
        )
        bad_patch = await client.patch(
            f"/api/v1/tasks/{task_id}",
            headers=headers,
            json={"defer_until": future},
        )
        empty_patch = await client.patch(
            f"/api/v1/tasks/{task_id}", headers=headers, json={}
        )
        snoozed = await client.post(
            f"/api/v1/tasks/{task_id}/snooze",
            headers=headers,
            json={"minutes": 30},
        )
        completed = await client.post(f"/api/v1/tasks/{task_id}/complete", headers=headers)
        cancelled = await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=headers)
        missing = await client.get(f"/api/v1/tasks/{uuid4()}", headers=headers)

    assert unauthorized.status_code == 401
    assert created.status_code == 201
    assert created.json()["status"] == "active"
    assert invalid.status_code == 422
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [task_id]
    assert patched.status_code == 200
    assert patched.json()["priority"] == 2
    assert bad_patch.status_code == 422  # 本地任务不接受延期（仅 pnkx 镜像）
    assert empty_patch.status_code == 422
    assert snoozed.status_code == 200
    assert completed.status_code == 200
    assert completed.json()["status"] == "done"
    assert cancelled.status_code == 409
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# TODO-01 反向推送底座：update_fields 与镜像永不本地触发
# ---------------------------------------------------------------------------


async def test_update_fields_priority_and_mirror_defer_guardrails(
    database: Database, user_id: UUID
) -> None:
    store = TaskStore(database)
    local = await store.create(
        user_id=user_id,
        kind=TaskKind.TASK,
        title="本地任务",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(hours=2)),
        now=NOW,
    )
    updated = await store.update_fields(user_id, local.id, priority=2, now=NOW)
    assert updated.priority == 2
    cleared = await store.update_fields(user_id, local.id, clear_priority=True, now=NOW)
    assert cleared.priority is None
    with pytest.raises(ValueError, match="延期仅支持外部镜像任务"):
        await store.update_fields(
            user_id, local.id, defer_until=NOW + timedelta(hours=3), now=NOW
        )
    with pytest.raises(ValueError, match="priority"):
        await store.update_fields(user_id, local.id, priority=9, now=NOW)

    # 镜像任务：延期时间必须在未来；优先级变更同时登记待推标记
    mirror_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(
            TaskItemRecord(
                id=mirror_id,
                user_id=user_id,
                kind="task",
                title="pnkx 镜像",
                status="active",
                trigger_type="time",
                trigger_config={"type": "time", "at": None, "repeat_kind": "once"},
                source="pnkx",
                source_ref="pnkx:88",
                privacy_level="L1",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with pytest.raises(ValueError, match="未来"):
        await store.update_fields(
            user_id, mirror_id, defer_until=NOW - timedelta(minutes=1), now=NOW
        )
    deferred = await store.update_fields(
        user_id,
        mirror_id,
        priority=3,
        defer_until=NOW + timedelta(hours=5),
        now=NOW,
    )
    assert deferred.priority == 3
    assert deferred.next_fire_at == NOW + timedelta(hours=5)
    async with database.sessions() as session:
        pending = await session.scalar(
            select(TaskItemRecord.pending_priority).where(TaskItemRecord.id == mirror_id)
        )
    assert pending == 3


async def test_claim_due_never_fires_pnkx_mirror(database: Database, user_id: UUID) -> None:
    """镜像的 next_fire_at 是待推送延期标记：到期也不会被调度器认领。"""
    mirror_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(
            TaskItemRecord(
                id=mirror_id,
                user_id=user_id,
                kind="task",
                title="pnkx 镜像",
                status="active",
                trigger_type="time",
                trigger_config={"type": "time", "at": None, "repeat_kind": "once"},
                next_fire_at=NOW - timedelta(minutes=1),
                source="pnkx",
                source_ref="pnkx:77",
                privacy_level="L1",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    claimed = await store_claim_probe(database, NOW + timedelta(minutes=1))
    assert all(task.id != mirror_id for task in claimed)


async def store_claim_probe(
    database: Database, moment: datetime
) -> list[ClaimedTask]:
    store = TaskStore(database)
    return await store.claim_due(now=moment)
