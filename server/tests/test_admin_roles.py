"""Admin 分级鉴权与多用户数据 scoping。

全局配置路由（默认 min_role=owner）仅业主会话或 admin token 可访问；
个人数据路由（min_role=member）允许任意活跃会话，但成员会话被强制
scope 到本人——query/body 传入他人 user_id 被覆盖，他人资源按 404 处理。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.api.admin_config import AdminTokenGuard, admin_access
from app.api.admin_tasks import create_admin_tasks_router
from app.auth import AuthService
from app.db import Base, Database, create_database
from app.memory import MemoryCandidate, MemoryOriginKind, MemoryStore, MemoryType
from app.schemas.common import PrivacyLevel
from app.tasks import TaskKind, TaskStore, TaskTrigger


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


async def _member_session(database: Database) -> tuple[str, UUID]:
    service = AuthService(database)
    login = await service.login_sso_provisioned(
        sub="member-a", display_name="成员A", owner_sub="owner-sub"
    )
    return login.access_token, login.principal.user_id


async def _owner_session(database: Database) -> tuple[str, UUID]:
    service = AuthService(database)
    login = await service.login_sso_provisioned(
        sub="owner-sub", display_name="业主", owner_sub="owner-sub"
    )
    return login.access_token, login.principal.user_id


def _guard_app(database: Database) -> FastAPI:
    """最小应用：直接暴露 owner-only 与 member 级守卫端点（与生产一致的
    router 级 dependencies + admin_access(request) 模式）。"""
    app = FastAPI()
    app.state.auth_service = AuthService(database)
    owner_router = APIRouter(dependencies=[Depends(AdminTokenGuard("admin-token"))])
    member_router = APIRouter(
        dependencies=[Depends(AdminTokenGuard("admin-token", min_role="member"))]
    )

    @owner_router.get("/owner-only")
    async def owner_only() -> dict[str, bool]:
        return {"ok": True}

    @member_router.get("/personal")
    async def personal(request: Request) -> dict[str, str]:
        scoped = admin_access(request).scoped_user_id(None)
        # 机器访问无 principal、显式 user_id 为 None 时允许空；会话访问必为本人
        return {"user_id": str(scoped) if scoped is not None else ""}

    app.include_router(owner_router)
    app.include_router(member_router)
    return app


async def test_admin_guard_roles(database: Database) -> None:
    member_token, member_user = await _member_session(database)
    owner_token, _ = await _owner_session(database)
    app = _guard_app(database)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # admin token 机器访问：两级都放行
        assert (
            await client.get("/owner-only", headers={"Authorization": "Bearer admin-token"})
        ).status_code == 200
        assert (
            await client.get("/personal", headers={"Authorization": "Bearer admin-token"})
        ).status_code == 200
        # 业主会话：两级都放行
        assert (
            await client.get("/owner-only", headers={"Authorization": f"Bearer {owner_token}"})
        ).status_code == 200
        # 成员会话：全局配置 403；个人数据 scope 到本人
        member_headers = {"Authorization": f"Bearer {member_token}"}
        assert (await client.get("/owner-only", headers=member_headers)).status_code == 403
        personal = await client.get("/personal", headers=member_headers)
        assert personal.status_code == 200
        assert personal.json()["user_id"] == str(member_user)
        # 无凭据 401
        assert (await client.get("/owner-only")).status_code == 401


async def test_admin_tasks_scoped_to_member(database: Database) -> None:
    member_token, member_user = await _member_session(database)
    owner_token, owner_user = await _owner_session(database)
    store = TaskStore(database)
    fire_at = datetime.now(UTC) + timedelta(hours=1)
    member_task = await store.create(
        user_id=member_user,
        kind=TaskKind.REMINDER,
        title="成员任务",
        trigger=TaskTrigger(type="time", at=fire_at),
    )
    owner_task = await store.create(
        user_id=owner_user,
        kind=TaskKind.REMINDER,
        title="业主任务",
        trigger=TaskTrigger(type="time", at=fire_at),
    )

    app = FastAPI()
    app.state.auth_service = AuthService(database)
    app.include_router(create_admin_tasks_router(store, admin_token="admin-token"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        member_headers = {"Authorization": f"Bearer {member_token}"}
        # 成员缺省只见本人任务
        listing = await client.get("/api/v1/admin/tasks", headers=member_headers)
        assert listing.status_code == 200
        items = listing.json()["items"]
        assert [item["id"] for item in items] == [str(member_task.id)]
        # 成员显式传他人 user_id 也被覆盖到本人
        spoofed = await client.get(
            "/api/v1/admin/tasks",
            params={"user_id": str(owner_user)},
            headers=member_headers,
        )
        assert [item["id"] for item in spoofed.json()["items"] ] == [str(member_task.id)]
        # 成员以业主 user_id 取消业主任务 → 404（scope 后查无此任务）
        denied = await client.post(
            f"/api/v1/admin/tasks/{owner_task.id}/cancel",
            params={"user_id": str(owner_user)},
            headers=member_headers,
        )
        assert denied.status_code == 404
        # 业主会话显式指定他人 user_id 检视 → 放行
        owner_headers = {"Authorization": f"Bearer {owner_token}"}
        inspect = await client.get(
            "/api/v1/admin/tasks",
            params={"user_id": str(member_user)},
            headers=owner_headers,
        )
        assert [item["id"] for item in inspect.json()["items"]] == [str(member_task.id)]
        # admin token 缺省为跨用户全量（运维视角）
        machine = await client.get(
            "/api/v1/admin/tasks", headers={"Authorization": "Bearer admin-token"}
        )
        assert len(machine.json()["items"]) == 2


async def test_memory_member_cannot_touch_others(database: Database) -> None:
    from app.api.admin_memory import create_admin_memory_router

    member_token, member_user = await _member_session(database)
    owner_token, owner_user = await _owner_session(database)
    store = MemoryStore(database)
    owner_memory = await store.add(
        MemoryCandidate(
            subject_kind="user",
            subject_key="user:self",
            origin_kind=MemoryOriginKind.MANUAL,
            type=MemoryType.SEMANTIC,
            content="业主的私密记忆",
            privacy_level=PrivacyLevel.L1,
            importance=0.6,
            sources=[],
        ),
        user_id=owner_user,
        actor="admin",
    )

    app = FastAPI()
    app.state.auth_service = AuthService(database)
    app.include_router(create_admin_memory_router(store, admin_token="admin-token"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        member_headers = {"Authorization": f"Bearer {member_token}"}
        # 成员列表不见业主记忆（scope 到本人）
        listing = await client.get("/api/v1/admin/memories", headers=member_headers)
        assert listing.json()["total"] == 0
        # 成员读取/删除业主记忆 ID → 404
        detail = await client.get(
            f"/api/v1/admin/memories/{owner_memory.id}", headers=member_headers
        )
        assert detail.status_code == 404
        delete = await client.delete(
            f"/api/v1/admin/memories/{owner_memory.id}", headers=member_headers
        )
        assert delete.status_code == 404
        # 成员创建记忆时 body 带他人 user_id → 落在自己名下
        created = await client.post(
            "/api/v1/admin/memories",
            json={
                "user_id": str(owner_user),
                "type": "semantic",
                "content": "成员自己的记忆",
            },
            headers=member_headers,
        )
        assert created.status_code == 201
        assert created.json()["user_id"] == str(member_user)
        # 业主会话可读取业主记忆
        owner_headers = {"Authorization": f"Bearer {owner_token}"}
        assert (
            await client.get(f"/api/v1/admin/memories/{owner_memory.id}", headers=owner_headers)
        ).status_code == 200
