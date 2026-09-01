from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import create_cognition_router
from app.auth import AuthService
from app.cognition import CognitiveStore, GoalKind, GoalStatus, GoalTracker
from app.cognition.goal_tracker import commitment_instruction
from app.db import (
    AppUserRecord,
    Base,
    ConversationRecord,
    Database,
    MessageRecord,
    create_database,
)
from app.ids import uuid7
from app.schemas.common import PrivacyLevel
from app.tasks.goal_scheduler import GoalReminderScheduler

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


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
        session.add(AppUserRecord(id=value, display_name="Goal owner", status="active"))
    return value


async def _make_message(database: Database, user_id: UUID) -> UUID:
    """承诺证据必须真实归属该用户：建会话 + 消息。"""
    conversation_id = uuid7()
    message_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(
            ConversationRecord(
                id=conversation_id,
                user_id=user_id,
                status="active",
                created_at=NOW,
                last_active_at=NOW,
            )
        )
        session.add(
            MessageRecord(
                id=message_id,
                conversation_id=conversation_id,
                turn_id=uuid7(),
                seq=1,
                role="user",
                content="我周五要交报告",
                privacy_level="L1",
                created_at=NOW,
            )
        )
    return message_id


class FakeBackend:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[object] = []

    async def complete(self, request: object) -> object:
        self.requests.append(request)
        return type("Result", (), {"text": self.text})()


# ---------------------------------------------------------------------------
# 提醒认领 / 稍后 / 忽略降频
# ---------------------------------------------------------------------------


async def test_goal_pre_due_and_due_remind_once_each(database: Database, user_id: UUID) -> None:
    store = CognitiveStore(database)
    goal = await store.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="周五前review合同",
        source_kind="manual",
        source_id="manual:1",
        due_at=NOW + timedelta(hours=12),
    )
    # 到期前 24h 窗口内：pre_due 触发一次
    first = await store.claim_due_goal_reminders(now=NOW)
    second = await store.claim_due_goal_reminders(now=NOW + timedelta(minutes=1))
    assert [item.goal.id for item in first] == [goal.id]
    assert first[0].phase == "pre_due"
    assert second == []
    # 到期后：due 再触发一次
    due = await store.claim_due_goal_reminders(now=NOW + timedelta(hours=13))
    assert [item.goal.id for item in due] == [goal.id]
    assert due[0].phase == "due"
    assert await store.claim_due_goal_reminders(now=NOW + timedelta(hours=14)) == []


async def test_goal_reminder_window_not_reached(database: Database, user_id: UUID) -> None:
    store = CognitiveStore(database)
    await store.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="下周体检",
        source_kind="manual",
        source_id="manual:2",
        due_at=NOW + timedelta(days=3),
    )
    assert await store.claim_due_goal_reminders(now=NOW) == []


async def test_goal_reminder_defer_blocks_until_passed(database: Database, user_id: UUID) -> None:
    store = CognitiveStore(database)
    goal = await store.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="交材料",
        source_kind="manual",
        source_id="manual:3",
        due_at=NOW + timedelta(hours=6),
    )
    deferred = await store.defer_goal_reminders(
        user_id, goal.id, until=NOW + timedelta(hours=8), now=NOW
    )
    assert deferred.reminder_defer_until == NOW + timedelta(hours=8)
    # 推迟后：即使到期也不提醒
    assert await store.claim_due_goal_reminders(now=NOW + timedelta(hours=7)) == []
    # 推迟闸门过后：直接走 due 相位（pre_due 窗口已过）
    claimed = await store.claim_due_goal_reminders(now=NOW + timedelta(hours=9))
    assert [item.goal.id for item in claimed] == [goal.id]
    assert claimed[0].phase == "due"
    with pytest.raises(ValueError, match="未来"):
        await store.defer_goal_reminders(
            user_id, goal.id, until=NOW - timedelta(minutes=1), now=NOW
        )


async def test_goal_reminder_ignore_defers_one_day(database: Database, user_id: UUID) -> None:
    store = CognitiveStore(database)
    goal = await store.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="还信用卡",
        source_kind="manual",
        source_id="manual:4",
        due_at=NOW + timedelta(hours=10),
    )
    ignored = await store.ignore_goal_reminder(user_id, goal.id, now=NOW)
    assert ignored.ignored_count == 1
    assert ignored.reminder_defer_until == NOW + timedelta(days=1)
    assert await store.claim_due_goal_reminders(now=NOW + timedelta(hours=2)) == []
    again = await store.ignore_goal_reminder(user_id, goal.id, now=NOW + timedelta(days=1))
    assert again.ignored_count == 2


async def test_completed_goal_not_reminded(database: Database, user_id: UUID) -> None:
    store = CognitiveStore(database)
    goal = await store.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="已完成的承诺",
        source_kind="manual",
        source_id="manual:5",
        due_at=NOW + timedelta(hours=1),
    )
    await store.set_goal_status(user_id=user_id, goal_id=goal.id, status=GoalStatus.COMPLETED)
    assert await store.claim_due_goal_reminders(now=NOW + timedelta(hours=2)) == []


async def test_goal_by_source_dedupe(database: Database, user_id: UUID) -> None:
    store = CognitiveStore(database)
    found = await store.goal_by_source(user_id, source_kind="manual", source_id="manual:x")
    assert found is None
    created = await store.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="唯一",
        source_kind="manual",
        source_id="manual:x",
    )
    found = await store.goal_by_source(user_id, source_kind="manual", source_id="manual:x")
    assert found is not None and found.id == created.id


# ---------------------------------------------------------------------------
# 承诺提取
# ---------------------------------------------------------------------------


async def test_tracker_creates_goal_from_explicit_commitment(
    database: Database, user_id: UUID
) -> None:
    store = CognitiveStore(database)
    message_id = await _make_message(database, user_id)
    backend = FakeBackend(
        '{"commitments":[{"title":"周五前提交报告","due_at":"2026-09-04T18:00:00+08:00","confidence":0.9}]}'
    )
    tracker = GoalTracker(store)
    created = await tracker.ingest_message(
        user_id=user_id,
        message_id=message_id,
        text="我周五一定要把报告交给老板",
        privacy_level=PrivacyLevel.L1,
        backend=backend,
    )
    assert len(created) == 1
    assert created[0].title == "周五前提交报告"
    assert created[0].source_kind == "message"
    assert created[0].source_id == str(message_id)
    assert created[0].due_at is not None
    # 同一消息再次提取：幂等不重复建
    again = await tracker.ingest_message(
        user_id=user_id,
        message_id=message_id,
        text="我周五一定要把报告交给老板",
        privacy_level=PrivacyLevel.L1,
        backend=FakeBackend('{"commitments":[{"title":"重复","due_at":null,"confidence":0.95}]}'),
    )
    assert again == []


async def test_tracker_filters_low_confidence_and_bad_output(
    database: Database, user_id: UUID
) -> None:
    store = CognitiveStore(database)
    tracker = GoalTracker(store)
    message_id = await _make_message(database, user_id)
    low = await tracker.ingest_message(
        user_id=user_id,
        message_id=uuid7(),
        text="可能会去健身",
        privacy_level=PrivacyLevel.L1,
        backend=FakeBackend('{"commitments":[{"title":"健身","due_at":null,"confidence":0.4}]}'),
    )
    assert low == []
    broken = await tracker.ingest_message(
        user_id=user_id,
        message_id=uuid7(),
        text="模型坏输出",
        privacy_level=PrivacyLevel.L1,
        backend=FakeBackend("not json at all"),
    )
    assert broken == []
    no_backend = await tracker.ingest_message(
        user_id=user_id,
        message_id=uuid7(),
        text="我明天要交报告",
        privacy_level=PrivacyLevel.L1,
        backend=None,
    )
    assert no_backend == []
    l3 = await tracker.ingest_message(
        user_id=user_id,
        message_id=uuid7(),
        text="我明天要交报告",
        privacy_level=PrivacyLevel.L3,
        backend=FakeBackend('{"commitments":[{"title":"x","due_at":null,"confidence":1.0}]}'),
    )
    assert l3 == []
    assert (
        await store.goal_by_source(user_id, source_kind="message", source_id=str(message_id))
        is None
    )


def test_commitment_instruction_l2_privacy_suffix() -> None:
    assert "隐私约束" in commitment_instruction(PrivacyLevel.L2)
    assert "隐私约束" not in commitment_instruction(PrivacyLevel.L1)


# ---------------------------------------------------------------------------
# 调度器与 API
# ---------------------------------------------------------------------------


class RecordingDeliverer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        text: str,
        *,
        user_id: UUID,
        goal_id: UUID,
        privacy_level: str,
        trigger_kind: str,
    ) -> list[str]:
        self.calls.append(
            {
                "text": text,
                "user_id": user_id,
                "goal_id": goal_id,
                "trigger_kind": trigger_kind,
            }
        )
        return ["web_chat"]


async def test_goal_scheduler_reminds_via_deliverer(database: Database, user_id: UUID) -> None:
    store = CognitiveStore(database)
    goal = await store.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="还书",
        source_kind="manual",
        source_id="manual:6",
        due_at=NOW + timedelta(hours=12),
    )
    deliverer = RecordingDeliverer()
    scheduler = GoalReminderScheduler(store, deliverer=deliverer)
    assert await scheduler.run_once(now=NOW) == 1
    assert len(deliverer.calls) == 1
    assert deliverer.calls[0]["goal_id"] == goal.id
    assert deliverer.calls[0]["trigger_kind"] == "goal.pre_due"
    assert "还书" in str(deliverer.calls[0]["text"])
    assert deliverer.calls[0]["user_id"] == user_id
    assert await scheduler.run_once(now=NOW + timedelta(minutes=5)) == 0
    assert await scheduler.run_once(now=NOW + timedelta(hours=13)) == 1
    assert deliverer.calls[1]["trigger_kind"] == "goal.due"


async def test_goal_reminder_feedback_api(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="Goal user", password="correct horse")
    store = CognitiveStore(database)
    goal = await store.create_goal(
        user_id=owner.principal.user_id,
        kind=GoalKind.USER,
        title="api 反馈",
        source_kind="manual",
        source_id="manual:api",
        due_at=datetime.now(UTC) + timedelta(hours=6),
    )
    app = FastAPI()
    app.include_router(create_cognition_router(store, auth))
    headers = {"Authorization": f"Bearer {owner.access_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ignored = await client.post(
            f"/api/v1/cognition/goals/{goal.id}/reminder-feedback",
            headers=headers,
            json={"kind": "ignored"},
        )
        snoozed = await client.post(
            f"/api/v1/cognition/goals/{goal.id}/reminder-feedback",
            headers=headers,
            json={"kind": "snoozed", "minutes": 30},
        )
        missing = await client.post(
            f"/api/v1/cognition/goals/{uuid7()}/reminder-feedback",
            headers=headers,
            json={"kind": "ignored"},
        )
        unauthorized = await client.post(
            f"/api/v1/cognition/goals/{goal.id}/reminder-feedback",
            json={"kind": "ignored"},
        )
    assert ignored.status_code == 200
    assert ignored.json()["ignored_count"] == 1
    assert snoozed.status_code == 200
    assert snoozed.json()["reminder_defer_until"] is not None
    assert missing.status_code == 404
    assert unauthorized.status_code == 401
