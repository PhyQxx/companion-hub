"""聊天端建提醒/建日程工具：写入契约、两段式确认、幂等与隐私门禁。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.calendar import CalendarCreateTool, CalendarService, CalendarStore
from app.chat import ChatService
from app.config import DatabaseConfigStore, HubConfig
from app.db import AppUserRecord, Base, Database, TaskItemRecord, create_database
from app.ids import uuid7
from app.schemas.common import PrivacyLevel
from app.tasks import ReminderCreateTool, TaskStore
from app.tools.contracts import ToolContext

NOW = datetime(2026, 9, 3, 4, 0, tzinfo=UTC)  # Asia/Shanghai 当天 12:00


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
        session.add(AppUserRecord(id=value, display_name="Tool owner", status="active"))
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


def _task_store(database: Database) -> TaskStore:
    return TaskStore(database)


def _calendar_service(database: Database) -> CalendarService:
    return CalendarService(CalendarStore(database), TaskStore(database), clock=_clock)


# ---------------------------------------------------------------------------
# ReminderCreateTool
# ---------------------------------------------------------------------------


class TestReminderCreateTool:
    async def test_creates_reminder_with_local_naive_time(
        self, database: Database, context: ToolContext
    ) -> None:
        tool = ReminderCreateTool(
            _task_store(database), timezone_name="Asia/Shanghai", clock=_clock
        )

        result = await tool.execute(
            ReminderCreateTool.arguments_model.model_validate(
                {"title": "晚上吃药", "at": "2026-09-03T20:00"}
            ),
            context,
        )

        assert result.ok is True
        assert result.data["created"] is True
        task = result.data["task"]
        assert task["next_fire_at"] == "2026-09-03T20:00:00+08:00"
        async with database.sessions() as session:
            rows = (await session.scalars(select(TaskItemRecord))).all()
        assert len(rows) == 1
        assert rows[0].title == "晚上吃药"
        assert rows[0].source == "chat"
        assert rows[0].privacy_level == "L1"

    async def test_maps_repeat_and_event_triggers(
        self, database: Database, context: ToolContext
    ) -> None:
        tool = ReminderCreateTool(
            _task_store(database), timezone_name="Asia/Shanghai", clock=_clock
        )

        daily = await tool.execute(
            ReminderCreateTool.arguments_model.model_validate(
                {"title": "喝水", "at": "2026-09-03T09:00", "repeat": "daily"}
            ),
            context,
        )
        assert daily.ok is True
        assert daily.data["task"]["repeat"] == "daily"

        turn = context.model_copy(update={"turn_id": uuid4()})
        event = await tool.execute(
            ReminderCreateTool.arguments_model.model_validate(
                {"title": "到家拿快递", "event": "user_arrived_home"}
            ),
            turn,
        )
        assert event.ok is True
        assert event.data["task"]["event"] == "user_arrived_home"
        assert event.data["task"]["next_fire_at"] is None

    async def test_same_turn_is_idempotent(
        self, database: Database, context: ToolContext
    ) -> None:
        tool = ReminderCreateTool(
            _task_store(database), timezone_name="Asia/Shanghai", clock=_clock
        )
        args = ReminderCreateTool.arguments_model.model_validate(
            {"title": "提醒", "at": "2026-09-03T15:00"}
        )

        first = await tool.execute(args, context)
        second = await tool.execute(args, context)

        assert first.data["created"] is True
        assert second.data["created"] is False
        assert second.data["duplicate"] is True
        assert second.data["task"]["id"] == first.data["task"]["id"]
        async with database.sessions() as session:
            rows = (await session.scalars(select(TaskItemRecord))).all()
        assert len(rows) == 1

    async def test_rejects_past_one_shot_trigger(
        self, database: Database, context: ToolContext
    ) -> None:
        tool = ReminderCreateTool(
            _task_store(database), timezone_name="Asia/Shanghai", clock=_clock
        )

        result = await tool.execute(
            ReminderCreateTool.arguments_model.model_validate(
                {"title": "过期", "at": "2020-01-01T08:00"}
            ),
            context,
        )

        assert result.ok is False
        assert result.reason_code == "invalid_trigger"
        assert result.data["created"] is False

    async def test_refuses_private_session_and_missing_turn(
        self, database: Database, context: ToolContext
    ) -> None:
        tool = ReminderCreateTool(
            _task_store(database), timezone_name="Asia/Shanghai", clock=_clock
        )
        args = ReminderCreateTool.arguments_model.model_validate(
            {"title": "私密", "at": "2026-09-03T15:00"}
        )

        private = await tool.execute(
            args, context.model_copy(update={"privacy_level": "L2"})
        )
        assert private.ok is False
        assert private.reason_code == "private_session_unsupported"

        anonymous = await tool.execute(args, context.model_copy(update={"turn_id": None}))
        assert anonymous.ok is False
        assert anonymous.reason_code == "idempotency_key_missing"


# ---------------------------------------------------------------------------
# CalendarCreateTool（两段式：先预览确认，再落库）
# ---------------------------------------------------------------------------


class TestCalendarCreateTool:
    async def test_first_call_returns_preview_without_confirm(
        self, database: Database, context: ToolContext
    ) -> None:
        tool = CalendarCreateTool(_calendar_service(database), timezone_name="Asia/Shanghai")

        result = await tool.execute(
            CalendarCreateTool.arguments_model.model_validate(
                {
                    "title": "项目评审",
                    "starts_at": "2026-09-04T14:00",
                    "ends_at": "2026-09-04T15:00",
                    "participants": ["小明"],
                }
            ),
            context,
        )

        assert result.ok is True
        assert result.data["created"] is False
        assert result.data["confirmation_required"] is True
        preview = result.data["preview"]
        assert preview["starts_at"] == "2026-09-04T14:00:00+08:00"
        assert preview["participants"] == ["小明"]
        assert preview["reminder_lead_minutes"] == 10

    async def test_confirmed_call_creates_event_and_reminder(
        self, database: Database, context: ToolContext
    ) -> None:
        tool = CalendarCreateTool(_calendar_service(database), timezone_name="Asia/Shanghai")

        result = await tool.execute(
            CalendarCreateTool.arguments_model.model_validate(
                {
                    "title": "项目评审",
                    "starts_at": "2026-09-04T14:00",
                    "ends_at": "2026-09-04T15:00",
                    "confirmed": True,
                }
            ),
            context,
        )

        assert result.ok is True
        assert result.data["created"] is True
        event = result.data["event"]
        assert event["reminder_task_id"]
        assert context.user_id is not None
        tasks = await _calendar_service(database).task_store.list_tasks(context.user_id)
        assert [task.source_ref for task in tasks] == [
            f"calendar:{event['id']}"
        ]

    async def test_conflict_blocks_creation_even_when_confirmed(
        self, database: Database, context: ToolContext
    ) -> None:
        service = _calendar_service(database)
        assert context.user_id is not None
        await service.create_event(
            context.user_id,
            title="既有会议",
            starts_at=NOW + timedelta(days=1),
            ends_at=NOW + timedelta(days=1, hours=1),
            reminder_lead_minutes=0,
        )
        tool = CalendarCreateTool(service, timezone_name="Asia/Shanghai")

        result = await tool.execute(
            CalendarCreateTool.arguments_model.model_validate(
                {
                    "title": "撞档的会",
                    "starts_at": (NOW + timedelta(days=1, minutes=30)).isoformat(),
                    "ends_at": (NOW + timedelta(days=1, minutes=90)).isoformat(),
                    "confirmed": True,
                }
            ),
            context,
        )

        assert result.ok is True
        assert result.data["created"] is False
        assert result.data["time_conflict"] is True
        assert result.data["conflicts"][0]["title"] == "既有会议"

    async def test_created_event_is_idempotent_within_turn(
        self, database: Database, context: ToolContext
    ) -> None:
        tool = CalendarCreateTool(_calendar_service(database), timezone_name="Asia/Shanghai")
        args = CalendarCreateTool.arguments_model.model_validate(
            {
                "title": "体检",
                "starts_at": "2026-09-05T09:00",
                "ends_at": "2026-09-05T10:00",
                "confirmed": True,
            }
        )

        first = await tool.execute(args, context)
        second = await tool.execute(args, context)

        assert first.data["created"] is True
        assert second.data["duplicate"] is True
        assert second.data["event"]["id"] == first.data["event"]["id"]

    async def test_rejects_inverted_window_and_private_session(
        self, database: Database, context: ToolContext
    ) -> None:
        tool = CalendarCreateTool(_calendar_service(database), timezone_name="Asia/Shanghai")

        inverted = await tool.execute(
            CalendarCreateTool.arguments_model.model_validate(
                {
                    "title": "倒置",
                    "starts_at": "2026-09-04T15:00",
                    "ends_at": "2026-09-04T14:00",
                    "confirmed": True,
                }
            ),
            context,
        )
        assert inverted.ok is False
        assert inverted.reason_code == "invalid_time_window"

        private = await tool.execute(
            CalendarCreateTool.arguments_model.model_validate(
                {
                    "title": "私密",
                    "starts_at": "2026-09-04T14:00",
                    "ends_at": "2026-09-04T15:00",
                }
            ),
            context.model_copy(update={"privacy_level": "L2"}),
        )
        assert private.ok is False
        assert private.reason_code == "private_session_unsupported"


# ---------------------------------------------------------------------------
# ChatService 挂载门禁：仅 L1 暴露，L0/L2 不挂载
# ---------------------------------------------------------------------------


def _config_yaml() -> str:
    return """
schema_version: 1
models:
  cloud:
    provider: openai_compatible
    model: dialogue-v1
    base_url: https://models.example/v1
    secret_ref: env:MODEL_API_KEY
    runs_local: false
    max_privacy_level: L1
    supports_tool_calling: true
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
  local:
    provider: openai_compatible
    model: local-model
    base_url: http://127.0.0.1:11434/v1
    runs_local: true
    max_privacy_level: L2
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
routes:
  dialogue: {primary: cloud}
  utility: {primary: cloud}
  private: {primary: local}
"""


@pytest.fixture
async def config_store(
    database: Database, tmp_path: Path
) -> AsyncIterator[DatabaseConfigStore]:
    path = tmp_path / "hub.yaml"
    path.write_text(_config_yaml(), encoding="utf-8")
    store = DatabaseConfigStore(database, path)
    await store.load()
    yield store


async def test_assistant_tools_mount_only_at_l1(
    database: Database, config_store: DatabaseConfigStore
) -> None:
    service = ChatService(
        database,
        config_store,
        device_tools=(
            ReminderCreateTool.__new__(ReminderCreateTool),
            CalendarCreateTool.__new__(CalendarCreateTool),
        ),
    )
    user = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user, display_name="Mount", status="active"))
    conversation = await service.create_conversation(user_id=user, title="tools")

    l1 = await service.start_turn(
        conversation.id, user_id=user, text="提醒我明早八点喝水", privacy_level=PrivacyLevel.L1
    )
    assert "reminder_create" in l1.tool_names
    assert "calendar_create" in l1.tool_names

    conversation_l0 = await service.create_conversation(user_id=user, title="l0")
    l0 = await service.start_turn(
        conversation_l0.id,
        user_id=user,
        text="提醒我明早八点喝水",
        privacy_level=PrivacyLevel.L0,
    )
    assert "reminder_create" not in l0.tool_names

    conversation_l2 = await service.create_conversation(user_id=user, title="l2")
    l2 = await service.start_turn(
        conversation_l2.id,
        user_id=user,
        text="提醒我明早八点喝水",
        privacy_level=PrivacyLevel.L2,
    )
    assert "reminder_create" not in l2.tool_names
    assert "calendar_create" not in l2.tool_names


async def test_assistant_tools_hidden_without_tool_capable_model(
    database: Database, config_store: DatabaseConfigStore
) -> None:
    candidate = config_store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = False
    draft = await config_store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await config_store.publish(draft.version, actor="test")
    service = ChatService(
        database,
        config_store,
        device_tools=(ReminderCreateTool.__new__(ReminderCreateTool),),
    )
    user = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user, display_name="NoTool", status="active"))
    conversation = await service.create_conversation(user_id=user, title="no-tools")

    pending = await service.start_turn(
        conversation.id, user_id=user, text="提醒我喝水", privacy_level=PrivacyLevel.L1
    )
    assert "reminder_create" not in pending.tool_names
