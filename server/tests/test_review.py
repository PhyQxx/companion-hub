from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import create_reviews_router
from app.auth import AuthService
from app.cognition import CognitiveStore, GoalKind, GoalStatus
from app.db import AppUserRecord, Base, Database, create_database
from app.ids import uuid7
from app.tasks.models import TaskKind, TaskTrigger
from app.tasks.review import DailyReviewService, compose_review_text
from app.tasks.review_scheduler import DailyReviewScheduler
from app.tasks.store import TaskStore

NOW = datetime(2026, 9, 2, 13, 0, tzinfo=UTC)  # Asia/Shanghai 当天 21:00


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
        session.add(AppUserRecord(id=value, display_name="Review owner", status="active"))
    return value


def _service(
    database: Database,
    *,
    clock: Callable[[], datetime] = lambda: NOW,
) -> DailyReviewService:
    return DailyReviewService(
        database,
        TaskStore(database),
        CognitiveStore(database),
        clock=clock,
    )


# ---------------------------------------------------------------------------
# 四区块采集
# ---------------------------------------------------------------------------


async def test_collect_items_covers_four_sections(database: Database, user_id: UUID) -> None:
    tasks = TaskStore(database)
    done_task = await tasks.create(
        user_id=user_id,
        kind=TaskKind.TASK,
        title="上午整理了发票",
        trigger=TaskTrigger(type="time", at=NOW - timedelta(hours=12)),
        now=NOW - timedelta(hours=13),
    )
    await tasks.complete_task(user_id, done_task.id, now=NOW - timedelta(hours=11))
    await tasks.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="逾期未做的体检预约",
        trigger=TaskTrigger(type="time", at=NOW - timedelta(days=1)),
        now=NOW - timedelta(days=2),
    )
    await tasks.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="明早站会",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(hours=12)),
        now=NOW,
    )
    goals = CognitiveStore(database)
    await goals.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="答应帮小王看方案",
        source_kind="manual",
        source_id="manual:r1",
        now=NOW,
    )  # 今日新建 → 新承诺
    due_tomorrow = await goals.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="明天到期还书",
        source_kind="manual",
        source_id="manual:r2",
        due_at=NOW + timedelta(hours=14),
        now=NOW - timedelta(days=1),
    )
    finished = await goals.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="完成了的承诺",
        source_kind="manual",
        source_id="manual:r3",
        now=NOW - timedelta(days=1),
    )
    await goals.set_goal_status(
        user_id=user_id, goal_id=finished.id, status=GoalStatus.COMPLETED, now=NOW
    )
    _ = due_tomorrow

    service = _service(database)
    items = await service.collect_items(user_id, review_date=date(2026, 9, 2))
    sections = {item.section for item in items}
    assert sections == {"completed", "unfinished", "new_commitment", "tomorrow"}
    completed_texts = [item.text for item in items if item.section == "completed"]
    assert "上午整理了发票" in completed_texts
    assert "完成了的承诺" in completed_texts
    unfinished = [item.text for item in items if item.section == "unfinished"]
    assert any("逾期" in text for text in unfinished)
    new = [item.text for item in items if item.section == "new_commitment"]
    assert new == ["答应帮小王看方案"]
    tomorrow = [item.text for item in items if item.section == "tomorrow"]
    assert any("明早站会" in text for text in tomorrow)
    assert any("明天到期还书" in text for text in tomorrow)


def test_compose_review_text_empty_and_removed() -> None:
    from app.tasks.review import ReviewItem

    text = compose_review_text(date(2026, 9, 2), [])
    assert text.endswith("今天没有需要回顾的事项。")

    items = [
        ReviewItem(section="completed", text="完成A", source="task:1"),
        ReviewItem(section="completed", text="误判B", source="task:2", action="removed"),
        ReviewItem(section="unfinished", text="未完C", source="task:3", note="其实已完成"),
    ]
    text = compose_review_text(date(2026, 9, 2), items)
    assert "完成A" in text
    assert "误判B" not in text
    assert "用户更正：其实已完成" in text


# ---------------------------------------------------------------------------
# 幂等 / 修正 / 投递
# ---------------------------------------------------------------------------


async def test_build_idempotent_and_correction_preserved(database: Database, user_id: UUID) -> None:
    service = _service(database)
    tasks = TaskStore(database)
    task = await tasks.create(
        user_id=user_id,
        kind=TaskKind.TASK,
        title="写周报",
        trigger=TaskTrigger(type="time", at=NOW - timedelta(hours=2)),
        now=NOW - timedelta(hours=3),
    )
    await tasks.complete_task(user_id, task.id, now=NOW - timedelta(hours=1))

    first = await service.build(user_id, review_date=date(2026, 9, 2))
    corrected = await service.correct_item(
        user_id,
        first.id,
        0,
        action="confirmed",
        note="其实还差结尾",
    )
    assert corrected.items[0].action == "confirmed"
    assert corrected.items[0].note == "其实还差结尾"
    assert "用户更正：其实还差结尾" in corrected.text
    # 同日再次生成不覆盖修正
    again = await service.build(user_id, review_date=date(2026, 9, 2))
    assert again.items[0].action == "confirmed"
    # 越界索引
    with pytest.raises(ValueError, match="out of range"):
        await service.correct_item(user_id, first.id, 9, action="removed")
    with pytest.raises(LookupError):
        await service.correct_item(uuid7(), first.id, 0, action="removed")


async def test_correction_does_not_touch_task_or_goal_state(
    database: Database, user_id: UUID
) -> None:
    """验收：用户修正只落在回顾自身，不擅自改任务/目标状态。"""
    service = _service(database)
    tasks = TaskStore(database)
    task = await tasks.create(
        user_id=user_id,
        kind=TaskKind.TASK,
        title="只读验证",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(hours=1)),
        now=NOW - timedelta(hours=2),
    )
    await tasks.complete_task(user_id, task.id, now=NOW - timedelta(minutes=30))
    review = await service.build(user_id, review_date=date(2026, 9, 2))
    await service.correct_item(user_id, review.id, 0, action="removed")
    task_view = await tasks.get_task(user_id, task.id)
    assert task_view.status == "done"  # 任务状态未被“复活”


async def test_deliver_once_per_evening(database: Database, user_id: UUID) -> None:
    service = _service(database)
    calls: list[str] = []

    async def deliverer(text: str, **kwargs: object) -> list[str]:
        calls.append(text)
        return ["web_chat"]

    delivered = await service.deliver(user_id, deliverer=deliverer, review_date=date(2026, 9, 2))
    again = await service.deliver(user_id, deliverer=deliverer, review_date=date(2026, 9, 2))
    assert delivered.status == "delivered"
    assert delivered.channels == ["web_chat"]
    assert again.status == "delivered"
    assert len(calls) == 1


async def test_review_scheduler_time_gate(database: Database, user_id: UUID) -> None:
    service = _service(database)
    delivered_texts: list[str] = []

    async def deliverer(text: str, **kwargs: object) -> list[str]:
        delivered_texts.append(text)
        return ["web_chat"]

    scheduler = DailyReviewScheduler(
        service,
        review_time=dt_time(21, 30),
        clock=lambda: NOW,
        deliverer=deliverer,
    )
    early = await scheduler.run_once(now=NOW - timedelta(minutes=31))
    assert early == 0
    assert await scheduler.run_once(now=NOW + timedelta(minutes=31)) == 1
    assert await scheduler.run_once(now=NOW + timedelta(minutes=40)) == 0
    assert len(delivered_texts) == 1


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


async def test_reviews_api_generate_latest_correct(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="Review user", password="correct horse")
    tasks = TaskStore(database)
    task = await tasks.create(
        user_id=owner.principal.user_id,
        kind=TaskKind.TASK,
        title="API 回顾项",
        trigger=TaskTrigger(type="time", at=datetime.now(UTC) + timedelta(hours=1)),
    )
    await tasks.complete_task(owner.principal.user_id, task.id)
    service = _service(database, clock=lambda: datetime.now(UTC))
    app = FastAPI()
    app.include_router(create_reviews_router(service, auth))
    headers = {"Authorization": f"Bearer {owner.access_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        empty = await client.get("/api/v1/reviews/latest", headers=headers)
        generated = await client.post("/api/v1/reviews/generate", headers=headers)
        review_id = generated.json()["id"]
        corrected = await client.patch(
            f"/api/v1/reviews/{review_id}/items/0",
            headers=headers,
            json={"action": "confirmed", "note": "确认无误"},
        )
        missing = await client.patch(
            f"/api/v1/reviews/{uuid7()}/items/0",
            headers=headers,
            json={"action": "confirmed"},
        )
        out_of_range = await client.patch(
            f"/api/v1/reviews/{review_id}/items/99",
            headers=headers,
            json={"action": "confirmed"},
        )
        unauthorized = await client.get("/api/v1/reviews/latest")
    assert empty.status_code == 200 and empty.json() is None
    assert generated.status_code == 200
    assert corrected.status_code == 200
    assert corrected.json()["items"][0]["action"] == "confirmed"
    assert missing.status_code == 404
    assert out_of_range.status_code == 422
    assert unauthorized.status_code == 401
