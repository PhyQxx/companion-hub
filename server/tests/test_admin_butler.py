"""Admin 管家中心 API：流程/场景/会议/简报/回顾的只读列表、汇总与安全操作。

红线：Admin 只读 + 删除流程模板/场景启停；运行、计划确认、会议授权与
摘要确认不提供 Admin 入口（须走聊天端确认流）。单用户部署（ID-01 暂缓）
下 user_id 缺省取第一个活跃用户。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.admin_butler import create_admin_butler_router
from app.calendar.store import CalendarStore
from app.cognition.action_plan import ActionPlanService
from app.cognition.action_registry import build_builtin_action_registry
from app.cognition.store import CognitiveStore
from app.db import AppUserRecord, Base, Database, create_database
from app.home_scene import HomeSceneService, HomeSceneStore
from app.home_scene.models import HomeSceneStep
from app.ids import uuid7
from app.meetings import MeetingService, MeetingStore
from app.meetings.models import MeetingSummary
from app.schemas.common import PrivacyLevel
from app.tasks import TaskStore
from app.tasks.brief import DailyBriefService
from app.tasks.review import DailyReviewService
from app.workflows import WorkflowService, WorkflowStep, WorkflowStore

AUTH = {"Authorization": "Bearer test-admin-token"}
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


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
        session.add(AppUserRecord(id=value, display_name="Butler owner", status="active"))
    return value


class _NoopSummarizer:
    # Admin 列表/详情不会触发摘要（只有 finish 才调用）；满足协议签名即可。
    async def summarize(self, **_: object) -> MeetingSummary:
        return MeetingSummary(summary="", decisions=[], action_items=[])


def _services(database: Database) -> tuple[
    WorkflowService, HomeSceneService, MeetingService, DailyBriefService, DailyReviewService
]:
    registry = build_builtin_action_registry()
    plans = ActionPlanService(database, registry)
    workflows = WorkflowService(WorkflowStore(database), registry, plans)
    scenes = HomeSceneService(HomeSceneStore(database), plans, registry)
    meetings = MeetingService(
        MeetingStore(database),
        CalendarStore(database),
        TaskStore(database),
        _NoopSummarizer(),
        clock=lambda: NOW,
    )
    briefs = DailyBriefService(
        database,
        TaskStore(database),
        CognitiveStore(database),
        clock=lambda: NOW,
    )
    reviews = DailyReviewService(
        database,
        TaskStore(database),
        CognitiveStore(database),
        clock=lambda: NOW,
    )
    return workflows, scenes, meetings, briefs, reviews


@pytest.fixture
def client_app(
    database: Database, user_id: UUID
) -> tuple[FastAPI, tuple[
    WorkflowService, HomeSceneService, MeetingService, DailyBriefService, DailyReviewService
]]:
    services = _services(database)
    app = FastAPI()
    app.include_router(
        create_admin_butler_router(
            database=database,
            workflows=services[0],
            scenes=services[1],
            meetings=services[2],
            briefs=services[3],
            reviews=services[4],
            admin_token="test-admin-token",
        )
    )
    return app, services


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_requires_admin_token(client_app: tuple[FastAPI, tuple[
    WorkflowService, HomeSceneService, MeetingService, DailyBriefService, DailyReviewService
]]) -> None:
    app, _ = client_app
    async with _client(app) as client:
        response = await client.get("/api/v1/admin/butler/summary")
    assert response.status_code == 401


async def test_summary_counts_seeded_butler_data(
    client_app: tuple[FastAPI, tuple[
        WorkflowService, HomeSceneService, MeetingService, DailyBriefService, DailyReviewService
    ]],
    user_id: UUID,
) -> None:
    app, (workflows, scenes, meetings, briefs, reviews) = client_app
    await workflows.save_workflow(
        user_id=user_id,
        name="上班流程",
        steps=[WorkflowStep(action_id="desktop.app.open", arguments={"app": "Safari"})],
    )
    await scenes.create_scene(
        user_id=user_id,
        name="回家模式",
        trigger="user_arrived_home",
        steps=[HomeSceneStep(action_id="desktop.notification.show", arguments={
            "title": "到家", "body": "欢迎回来", "privacy_level": "L0",
        })],
    )
    await meetings.prepare(
        user_id,
        title="周会",
        participants=["我", "同事"],
        privacy_level=PrivacyLevel.L1,
    )
    await briefs.build(user_id)
    await reviews.build(user_id)
    async with _client(app) as client:
        response = await client.get("/api/v1/admin/butler/summary", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(user_id)
    assert body["workflows"] == 1
    assert body["scenes"] == 1
    assert body["active_scenes"] == 1
    assert body["meetings"] == 1
    assert body["briefs"] == 1
    assert body["reviews"] == 1
    assert body["latest_brief_date"]
    assert body["latest_review_date"]


async def test_workflow_list_and_delete(
    client_app: tuple[FastAPI, tuple[
        WorkflowService, HomeSceneService, MeetingService, DailyBriefService, DailyReviewService
    ]],
    user_id: UUID,
) -> None:
    app, (workflows, *_rest) = client_app
    created = await workflows.save_workflow(
        user_id=user_id,
        name="夜间模式",
        steps=[WorkflowStep(action_id="system.volume.set", arguments={"volume": 20})],
    )
    async with _client(app) as client:
        listed = await client.get("/api/v1/admin/butler/workflows", headers=AUTH)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [str(created.id)]
        assert listed.json()[0]["steps"][0]["action_id"] == "system.volume.set"

        deleted = await client.delete(
            f"/api/v1/admin/butler/workflows/{created.id}", headers=AUTH
        )
        assert deleted.status_code == 204
        missing = await client.delete(
            f"/api/v1/admin/butler/workflows/{created.id}", headers=AUTH
        )
        assert missing.status_code == 404
        empty = await client.get("/api/v1/admin/butler/workflows", headers=AUTH)
        assert empty.json() == []


async def test_scene_enable_disable_and_delete(
    client_app: tuple[FastAPI, tuple[
        WorkflowService, HomeSceneService, MeetingService, DailyBriefService, DailyReviewService
    ]],
    user_id: UUID,
) -> None:
    app, (_, scenes, *_) = client_app
    created = await scenes.create_scene(
        user_id=user_id,
        name="离家模式",
        trigger="manual",
        steps=[HomeSceneStep(action_id="desktop.notification.show", arguments={
            "title": "离家", "body": "已执行", "privacy_level": "L0",
        })],
    )
    async with _client(app) as client:
        disabled = await client.post(
            f"/api/v1/admin/butler/scenes/{created.id}/disable", headers=AUTH
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False

        enabled = await client.post(
            f"/api/v1/admin/butler/scenes/{created.id}/enable", headers=AUTH
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True

        unknown = await client.post(
            f"/api/v1/admin/butler/scenes/{uuid7()}/enable", headers=AUTH
        )
        assert unknown.status_code == 404

        deleted = await client.delete(
            f"/api/v1/admin/butler/scenes/{created.id}", headers=AUTH
        )
        assert deleted.status_code == 204
        assert await scenes.list_scenes(user_id) == []


async def test_meetings_briefs_reviews_listing(
    client_app: tuple[FastAPI, tuple[
        WorkflowService, HomeSceneService, MeetingService, DailyBriefService, DailyReviewService
    ]],
    user_id: UUID,
) -> None:
    app, (_, _, meetings, briefs, reviews) = client_app
    meeting = await meetings.prepare(
        user_id,
        title="评审会",
        participants=["我"],
        privacy_level=PrivacyLevel.L2,
    )
    brief = await briefs.build(user_id)
    review = await reviews.build(user_id)
    async with _client(app) as client:
        meeting_list = await client.get("/api/v1/admin/butler/meetings", headers=AUTH)
        assert meeting_list.status_code == 200
        assert [item["id"] for item in meeting_list.json()] == [str(meeting.id)]
        assert meeting_list.json()[0]["status"] == "prepared"

        brief_list = await client.get("/api/v1/admin/butler/briefs", headers=AUTH)
        assert [item["id"] for item in brief_list.json()] == [str(brief.id)]

        review_list = await client.get("/api/v1/admin/butler/reviews", headers=AUTH)
        assert [item["id"] for item in review_list.json()] == [str(review.id)]


async def test_explicit_user_id_and_empty_user_pool(database: Database) -> None:
    """显式 user_id 覆盖默认解析；没有任何活跃用户时 404。"""
    services = _services(database)
    app = FastAPI()
    app.include_router(
        create_admin_butler_router(
            database=database,
            workflows=services[0],
            scenes=services[1],
            meetings=services[2],
            briefs=services[3],
            reviews=services[4],
            admin_token="test-admin-token",
        )
    )
    async with _client(app) as client:
        no_user = await client.get("/api/v1/admin/butler/summary", headers=AUTH)
        assert no_user.status_code == 404

        other = uuid7()
        async with database.sessions.begin() as session:
            session.add(AppUserRecord(id=other, display_name="Second", status="active"))
        explicit = await client.get(
            "/api/v1/admin/butler/workflows", params={"user_id": str(other)}, headers=AUTH
        )
        assert explicit.status_code == 200
        assert explicit.json() == []

        default = await client.get("/api/v1/admin/butler/summary", headers=AUTH)
        assert default.status_code == 200
        assert default.json()["user_id"] == str(other)
