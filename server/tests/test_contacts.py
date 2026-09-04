"""CONTACT-01 联系人：存储唯一性、工具契约、隐私门禁、简报接入与 API 全链。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import create_contacts_router
from app.auth import AuthService
from app.cognition import CognitiveStore
from app.contacts import (
    ContactImportantDate,
    ContactPreference,
    ContactQueryTool,
    ContactSaveTool,
    ContactStore,
)
from app.contacts.models import days_until, next_occurrence
from app.contacts.tools import ContactQueryArgs, ContactSaveArgs
from app.db import AppUserRecord, Base, Database, create_database
from app.ids import uuid7
from app.schemas.common import PrivacyLevel
from app.tasks.brief import DailyBriefService, compose_brief_text
from app.tasks.store import TaskStore
from app.tools.contracts import ToolContext

NOW = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)  # Asia/Shanghai 当天 12:00


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
        session.add(AppUserRecord(id=value, display_name="Contact owner", status="active"))
    return value


@pytest.fixture
def context(user_id: UUID) -> ToolContext:
    return ToolContext(
        privacy_level=PrivacyLevel.L1,
        user_id=user_id,
        turn_id=UUID("0198b2f4-3b00-7001-8000-000000000001"),
    )


def _clock() -> datetime:
    return NOW


def _store(database: Database) -> ContactStore:
    return ContactStore(database)


# ---------------------------------------------------------------------------
# 存储与契约校验
# ---------------------------------------------------------------------------


async def test_create_normalizes_and_finds_by_alias(database: Database, user_id: UUID) -> None:
    store = _store(database)
    view = await store.create_contact(
        user_id=user_id,
        display_name="王小雨",
        aliases=["小雨", "小雨 ", "Rain"],
        relationship="大学室友",
        timezone=" Asia/Tokyo ",
        important_dates=[ContactImportantDate(label="生日", month=3, day=14)],
        preferences=[ContactPreference(key="饮食", value="不吃香菜")],
    )
    assert view.aliases == ["小雨", "Rain"]  # 去重 + 去空白
    assert view.timezone == "Asia/Tokyo"
    matched = await store.find_by_name(user_id, "rain")
    assert matched is not None and matched.id == view.id
    assert await store.find_by_name(user_id, "不存在") is None


async def test_name_and_alias_conflicts_rejected(database: Database, user_id: UUID) -> None:
    store = _store(database)
    await store.create_contact(user_id=user_id, display_name="妈妈", aliases=["母亲", "Rain"])
    # 别名与既有主名称冲突
    with pytest.raises(ValueError, match="已被"):
        await store.create_contact(user_id=user_id, display_name="新联系人", aliases=["妈妈"])
    # 主名称与既有别名冲突（casefold 忽略大小写）
    with pytest.raises(ValueError, match="已被"):
        await store.create_contact(user_id=user_id, display_name="rain")


async def test_invalid_timezone_and_dates_rejected(database: Database, user_id: UUID) -> None:
    store = _store(database)
    with pytest.raises(ValueError, match="未知时区"):
        await store.create_contact(user_id=user_id, display_name="A", timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        ContactImportantDate(label="生日", month=2, day=30)
    with pytest.raises(ValidationError):
        ContactImportantDate(label="生日", month=13, day=1)


async def test_update_replaces_fields_and_clears_timezone(
    database: Database, user_id: UUID
) -> None:
    store = _store(database)
    view = await store.create_contact(
        user_id=user_id,
        display_name="李雷",
        aliases=["雷子"],
        timezone="Asia/Shanghai",
        preferences=[ContactPreference(key="称呼", value="李工")],
    )
    updated = await store.update_contact(
        user_id,
        view.id,
        aliases=["老李"],
        timezone="",  # 清除时区
        important_dates=[ContactImportantDate(label="纪念日", month=10, day=1)],
    )
    assert updated.aliases == ["老李"]
    assert updated.timezone is None
    assert updated.display_name == "李雷"  # 未传则不变
    assert updated.important_dates[0].label == "纪念日"
    assert updated.preferences[0].value == "李工"  # 未传列表整体不变

    # 自我更新不触发自身别名冲突
    again = await store.update_contact(user_id, view.id, display_name="李雷", aliases=["老李"])
    assert again.display_name == "李雷"


async def test_delete_and_user_isolation(database: Database, user_id: UUID) -> None:
    store = _store(database)
    other = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=other, display_name="Other", status="active"))
    view = await store.create_contact(user_id=user_id, display_name="韩梅梅")
    with pytest.raises(LookupError):
        await store.get_contact_view(other, view.id)
    with pytest.raises(LookupError):
        await store.delete_contact(other, view.id)
    await store.delete_contact(user_id, view.id)
    with pytest.raises(LookupError):
        await store.get_contact_view(user_id, view.id)
    # 删除后名称可复用
    recreated = await store.create_contact(user_id=user_id, display_name="韩梅梅")
    assert recreated.id != view.id


def test_next_occurrence_helpers() -> None:
    today = date(2026, 9, 4)
    assert ContactImportantDate(label="生日", month=9, day=4).month == 9
    assert next_occurrence(today, 9, 4) == today
    assert next_occurrence(today, 9, 3) == date(2027, 9, 3)
    assert next_occurrence(today, 2, 29) == date(2028, 2, 29)  # 非闰年顺延到闰年
    assert days_until(today, 9, 14) == 10
    assert days_until(today, 9, 4) == 0


async def test_contacts_with_date_filters_month_day(database: Database, user_id: UUID) -> None:
    store = _store(database)
    await store.create_contact(
        user_id=user_id,
        display_name="妈妈",
        important_dates=[ContactImportantDate(label="生日", month=9, day=4)],
    )
    await store.create_contact(
        user_id=user_id,
        display_name="爸爸",
        important_dates=[ContactImportantDate(label="生日", month=12, day=4)],
    )
    matched = await store.contacts_with_date(user_id, month=9, day=4)
    assert [view.display_name for view in matched] == ["妈妈"]


# ---------------------------------------------------------------------------
# 聊天工具
# ---------------------------------------------------------------------------


async def test_contact_save_creates_then_idempotent_per_turn(
    database: Database, user_id: UUID, context: ToolContext
) -> None:
    tool = ContactSaveTool(_store(database), clock=_clock)
    args = ContactSaveArgs(
        display_name="妈妈",
        aliases=["母亲"],
        relationship="用户母亲",
        timezone="Asia/Shanghai",
        important_dates=[ContactImportantDate(label="生日", month=9, day=14)],
        preferences=[ContactPreference(key="饮食", value="口味清淡")],
    )
    first = await tool.execute(args, context)
    assert first.ok and first.data["created"] is True
    second = await tool.execute(args, context)
    assert second.ok and second.data["duplicate"] is True
    assert first.data["id"] == second.data["id"]
    listings = await _store(database).list_contacts(user_id)
    assert len(listings) == 1  # 同回合幂等，不重复建


async def test_contact_save_updates_existing_by_name(
    database: Database, user_id: UUID, context: ToolContext
) -> None:
    store = _store(database)
    await store.create_contact(user_id=user_id, display_name="妈妈")
    tool = ContactSaveTool(store, clock=_clock)
    result = await tool.execute(
        ContactSaveArgs(display_name="妈妈", timezone="America/New_York"), context
    )
    assert result.ok and result.data["created"] is False
    assert result.data["timezone"] == "America/New_York"


async def test_contact_save_privacy_and_idempotency_gates(
    database: Database, context: ToolContext
) -> None:
    tool = ContactSaveTool(_store(database), clock=_clock)
    private = ToolContext(
        privacy_level=PrivacyLevel.L2, user_id=context.user_id, turn_id=context.turn_id
    )
    rejected = await tool.execute(ContactSaveArgs(display_name="A"), private)
    assert not rejected.ok and rejected.reason_code == "private_session_unsupported"

    no_turn = ToolContext(privacy_level=PrivacyLevel.L1, user_id=context.user_id, turn_id=None)
    missing = await tool.execute(ContactSaveArgs(display_name="A"), no_turn)
    assert not missing.ok and missing.reason_code == "idempotency_key_missing"

    invalid = await tool.execute(ContactSaveArgs(display_name="B", timezone="Bad/Zone"), context)
    assert not invalid.ok and invalid.reason_code == "invalid_contact"


async def test_contact_query_returns_context_with_local_time(
    database: Database, user_id: UUID, context: ToolContext
) -> None:
    store = _store(database)
    await store.create_contact(
        user_id=user_id,
        display_name="王小雨",
        aliases=["小雨"],
        relationship="大学室友",
        timezone="Asia/Tokyo",
        important_dates=[ContactImportantDate(label="生日", month=9, day=14)],
    )
    tool = ContactQueryTool(store, clock=_clock)
    result = await tool.execute(ContactQueryArgs(name="小雨"), context)
    assert result.ok and result.data["found"] is True
    contact = result.data["contacts"][0]
    assert contact["display_name"] == "王小雨"
    assert contact["relationship"] == "大学室友"
    assert "local_time" in contact
    assert contact["date_countdowns"] == [{"label": "生日", "days_until": 10}]

    empty = await tool.execute(ContactQueryArgs(name="陌生人"), context)
    assert empty.ok and empty.data["found"] is False and empty.data["contacts"] == []

    private = ToolContext(
        privacy_level=PrivacyLevel.L2, user_id=context.user_id, turn_id=context.turn_id
    )
    rejected = await tool.execute(ContactQueryArgs(name="小雨"), private)
    assert not rejected.ok and rejected.reason_code == "private_session_unsupported"


# ---------------------------------------------------------------------------
# 简报接入
# ---------------------------------------------------------------------------


async def test_brief_includes_contact_important_dates(database: Database, user_id: UUID) -> None:
    store = _store(database)
    await store.create_contact(
        user_id=user_id,
        display_name="妈妈",
        important_dates=[ContactImportantDate(label="生日", month=9, day=4)],
    )
    service = DailyBriefService(
        database,
        TaskStore(database),
        CognitiveStore(database),
        contact_store=store,
        clock=_clock,
    )
    brief = await service.build(user_id, brief_date=date(2026, 9, 4))
    kinds = [fact.kind for fact in brief.facts]
    assert "contact_date" in kinds
    fact = next(item for item in brief.facts if item.kind == "contact_date")
    assert fact.text == "今天是妈妈的生日"
    assert fact.source.startswith("contact:")

    text = compose_brief_text(date(2026, 9, 4), brief.facts)
    assert "今日重要日期" in text
    assert "今天是妈妈的生日" in text
    # 只有重要日期、无任务/承诺时不输出“没有到期”短句
    assert "没有到期的任务或承诺" not in text


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


async def test_contacts_api_full_flow(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="Contacts user", password="correct horse")
    app = FastAPI()
    app.include_router(create_contacts_router(_store(database), auth))
    headers = {"Authorization": f"Bearer {owner.access_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/contacts")).status_code == 401

        created = await client.post(
            "/api/v1/contacts",
            headers=headers,
            json={
                "display_name": "王小雨",
                "aliases": ["小雨"],
                "relationship": "大学室友",
                "timezone": "Asia/Tokyo",
                "important_dates": [{"label": "生日", "month": 3, "day": 14}],
                "preferences": [{"key": "饮食", "value": "不吃香菜"}],
            },
        )
        assert created.status_code == 201
        contact_id = created.json()["id"]

        invalid = await client.post(
            "/api/v1/contacts",
            headers=headers,
            json={"display_name": "X", "timezone": "Mars/Olympus"},
        )
        assert invalid.status_code == 422

        conflict = await client.post(
            "/api/v1/contacts", headers=headers, json={"display_name": "小雨"}
        )
        assert conflict.status_code == 422

        searched = await client.get("/api/v1/contacts", headers=headers, params={"q": "雨"})
        assert [item["display_name"] for item in searched.json()] == ["王小雨"]

        fetched = await client.get(f"/api/v1/contacts/{contact_id}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["timezone"] == "Asia/Tokyo"

        patched = await client.patch(
            f"/api/v1/contacts/{contact_id}",
            headers=headers,
            json={"timezone": "", "aliases": ["Rain"]},
        )
        assert patched.status_code == 200
        assert patched.json()["timezone"] is None
        assert patched.json()["aliases"] == ["Rain"]

        deleted = await client.delete(f"/api/v1/contacts/{contact_id}", headers=headers)
        assert deleted.status_code == 204
        gone = await client.get(f"/api/v1/contacts/{contact_id}", headers=headers)
        assert gone.status_code == 404
