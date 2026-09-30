"""技能写链路巡检：空参数/未放行/连接缺失 → skill_suggestion（去重）。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.db import Base, Database, create_database
from app.skills.audit import SkillAuditScheduler, audit_skill
from app.skills.connections import SkillConnection, SkillConnectionStore
from app.skills.models import SkillApiManifest, SkillDocument, SkillOperation
from app.skills.store import SkillStore


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield value
    await value.close()


def _document(*, with_params: bool, path_allowlisted: bool) -> SkillDocument:
    parameters = (
        {"title": {"type": "string", "required": True, "location": "body"}} if with_params else {}
    )
    return SkillDocument(
        name="demo-skill",
        description="演示技能",
        instructions="说明",
        api=SkillApiManifest(
            schema_version=1,
            connection="demo-conn",
            operations=[
                SkillOperation(
                    name="thing_add",
                    description="新增",
                    method="POST",
                    path="/demo/thing" if path_allowlisted else "/demo/other",
                    risk="confirm",
                    parameters=parameters,
                )
            ],
        ),
    )


_WRITE_CONNECTION = dict(
    id="demo-conn",
    base_url="https://demo.example",
    auth_type="none",
    allowed_paths=["/demo/thing"],
    allowed_write_paths=["/demo/thing"],
    enabled=True,
)


async def test_audit_flags_empty_params_and_unallowlisted_path(database: Database) -> None:
    store = SkillStore(database)
    connections = SkillConnectionStore(database)
    await connections.put(SkillConnection(**_WRITE_CONNECTION))
    skill = await store.create(_document(with_params=False, path_allowlisted=False), source="t")
    await store.set_enabled(skill.id, True)
    scheduler = SkillAuditScheduler(store, connections)

    created = await scheduler.run_once()

    assert created == 2  # 空参数 + 路径未放行
    suggestions = await store.suggestions()
    reasons = {item.reason_code for item in suggestions}
    assert reasons == {
        "audit_write_op_without_params",
        "audit_write_path_not_allowlisted",
    }
    # 修复后（参数补齐、路径放行）：当前版本不再有发现，零新增
    fixed = _document(with_params=True, path_allowlisted=True)
    await store.set_enabled(skill.id, False)
    await store.set_api(skill.id, fixed.api or skill.api)  # type: ignore[arg-type]
    await store.set_enabled(skill.id, True)
    assert await scheduler.run_once() == 0


async def test_audit_dedupes_within_same_version(database: Database) -> None:
    store = SkillStore(database)
    connections = SkillConnectionStore(database)
    await connections.put(SkillConnection(**_WRITE_CONNECTION))
    skill = await store.create(_document(with_params=False, path_allowlisted=True), source="t")
    await store.set_enabled(skill.id, True)
    scheduler = SkillAuditScheduler(store, connections)

    assert await scheduler.run_once() == 1
    assert await scheduler.run_once() == 0  # 同版本同问题去重


async def test_audit_connection_disabled_short_circuits(database: Database) -> None:
    store = SkillStore(database)
    connections = SkillConnectionStore(database)
    disabled = {**_WRITE_CONNECTION, "enabled": False}
    await connections.put(SkillConnection(**disabled))
    await store.create(_document(with_params=False, path_allowlisted=False), source="t")
    view = await store.get_by_name("demo-skill")
    assert view is not None
    findings = audit_skill(view, connection_enabled=False, write_paths=frozenset())
    assert [item.reason_code for item in findings] == ["audit_connection_missing"]
