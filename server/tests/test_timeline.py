# ruff: noqa: RUF001
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from test_chat import FakeRouter, config_yaml

from app.bus import append_event
from app.chat import ChatService
from app.config import DatabaseConfigStore
from app.db import (
    AppUserRecord,
    Base,
    ConversationRecord,
    Database,
    MessageRecord,
    create_database,
)
from app.ids import uuid7
from app.llm import CompletionRequest
from app.main import create_app
from app.memory import (
    MemoryCandidate,
    MemorySourceKind,
    MemorySourceRef,
    MemoryStore,
    MemorySubjectKind,
    MemoryType,
)
from app.schemas import InputEnvelope, PrivacyLevel
from app.schemas.common import SourceRef
from app.timeline import (
    HistoryRecallService,
    RecallMode,
    ScreenActivityRecallService,
    TemporalQueryParser,
    TimelineActor,
    TimelineSourceType,
    TimelineStore,
    has_screen_activity_intent,
)


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


@pytest.fixture
async def user(database: Database) -> AppUserRecord:
    record = AppUserRecord(id=uuid7(), display_name="Timeline", status="active")
    async with database.sessions.begin() as session:
        session.add(record)
    return record


async def _chat_service(
    database: Database,
    tmp_path: Path,
    requests: list[CompletionRequest],
    *,
    memory_store: MemoryStore | None = None,
) -> tuple[ChatService, TimelineStore]:
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    config_store = DatabaseConfigStore(database, config_path)
    await config_store.load()
    timeline = TimelineStore(database)
    return (
        ChatService(
            database,
            config_store,
            router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
            memory_store=memory_store,
            timeline_store=timeline,
            history_recall_service=HistoryRecallService(timeline),
        ),
        timeline,
    )


def test_temporal_query_parser_resolves_relative_ranges() -> None:
    parser = TemporalQueryParser("Asia/Shanghai")
    now = datetime(2026, 8, 19, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    yesterday = parser.parse("我昨天晚上说了什么", now=now)
    assert yesterday is not None
    assert yesterday.start_at == datetime(
        2026, 8, 18, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert yesterday.end_at == datetime(
        2026, 8, 19, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )

    last_week = parser.parse("上周聊过什么", now=now)
    assert last_week is not None
    assert last_week.start_at.weekday() == 0
    assert last_week.end_at.weekday() == 0
    assert last_week.end_at - last_week.start_at == timedelta(days=7)

    last_night = parser.parse("昨晚我提到什么电影", now=now)
    assert last_night is not None
    assert last_night.start_at.hour == 18
    assert last_night.end_at.hour == 6

    invalid = TemporalQueryParser("Mars/Olympus")
    assert invalid.timezone_name == "Asia/Shanghai"


def test_screen_activity_intent_and_time_range_cover_natural_summary_request() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 28, 9, 42, tzinfo=timezone)
    query = "总结我今天上午在电脑上做了什么。"

    assert has_screen_activity_intent(query) is True
    temporal = TemporalQueryParser("Asia/Shanghai").parse(query, now=now)
    assert temporal is not None
    assert temporal.start_at == datetime(2026, 8, 28, 5, 0, tzinfo=timezone)
    assert temporal.end_at == now + timedelta(seconds=1)

    recent = TemporalQueryParser("Asia/Shanghai").parse(
        "总结过去2小时的电脑活动", now=now
    )
    assert recent is not None
    assert recent.start_at == now - timedelta(hours=2)
    assert recent.end_at == now + timedelta(seconds=1)


async def test_screen_activity_recall_filters_and_aggregates_observations(
    database: Database, user: AppUserRecord
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone)
    timeline = TimelineStore(database)
    await timeline.index_screen_observation(
        user_id=user.id,
        observation_id=uuid7(),
        display=1,
        summary="正在使用浏览器查看 companion-hub 项目代码",
        privacy_level=PrivacyLevel.L1,
        occurred_at=datetime(2026, 8, 28, 8, 0, tzinfo=timezone),
    )
    await timeline.index_screen_observation(
        user_id=user.id,
        observation_id=uuid7(),
        display=1,
        summary="正在使用浏览器查看 companion-hub 项目的代码",
        privacy_level=PrivacyLevel.L1,
        occurred_at=datetime(2026, 8, 28, 8, 5, tzinfo=timezone),
    )
    await timeline.index_screen_observation(
        user_id=user.id,
        observation_id=uuid7(),
        display=1,
        summary="正在企业微信回复工作消息",
        privacy_level=PrivacyLevel.L1,
        occurred_at=datetime(2026, 8, 28, 9, 0, tzinfo=timezone),
    )
    # 同一时间窗内的普通聊天消息不能混入屏幕活动总结。
    await timeline.index_message(
        user_id=user.id,
        conversation_id=uuid7(),
        message_id=uuid7(),
        actor=TimelineActor.USER,
        text="这是一条不应进入屏幕总结的聊天消息",
        privacy_level=PrivacyLevel.L1,
        occurred_at=datetime(2026, 8, 28, 8, 30, tzinfo=timezone),
    )

    recalled = await ScreenActivityRecallService(timeline).recall(
        "总结我今天上午在电脑上做了什么。",
        user_id=user.id,
        privacy_level=PrivacyLevel.L1,
        now=now,
        timezone_name="Asia/Shanghai",
    )

    assert recalled is not None
    assert len(recalled.events) == 3
    assert len(recalled.segments) == 2
    assert recalled.segments[0].observation_count == 2
    assert recalled.segments[1].summaries == ("正在企业微信回复工作消息",)
    context = ScreenActivityRecallService.render_context(
        recalled, timezone_name="Asia/Shanghai"
    )
    assert "【屏幕活动回顾】" in context
    assert "companion-hub" in context
    assert "不应进入屏幕总结" not in context


async def test_chat_injects_screen_activity_recall_and_records_meta(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    requests: list[CompletionRequest] = []
    service, timeline = await _chat_service(database, tmp_path, requests)
    observed_at = datetime.now(UTC) - timedelta(minutes=30)
    await timeline.index_screen_observation(
        user_id=user.id,
        observation_id=uuid7(),
        display=1,
        summary="正在 IDE 中修改屏幕事件聚合检索代码",
        privacy_level=PrivacyLevel.L1,
        occurred_at=observed_at,
    )
    conversation = await service.create_conversation(user_id=user.id, title="screen-recall")

    turn = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="总结我过去2小时在电脑上做了什么。",
        privacy_level=PrivacyLevel.L1,
    )

    system_prompt = requests[-1].messages[0].content
    assert "【屏幕活动回顾】" in system_prompt
    assert "修改屏幕事件聚合检索代码" in system_prompt
    recall_meta = (turn.assistant_message.decision_meta or {})["recall"]
    assert isinstance(recall_meta, dict)
    assert recall_meta["mode"] == "screen_activity"
    assert recall_meta["timeline_ids"]
    assert recall_meta["segment_count"] == 1


async def test_yesterday_evening_recall_finds_original_message_source(
    database: Database, user: AppUserRecord
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    conversation_id = uuid7()
    message_id = uuid7()
    turn_id = uuid7()
    occurred_local = datetime(2026, 8, 18, 21, 30, tzinfo=timezone)
    occurred_utc = occurred_local.astimezone(UTC)
    async with database.sessions.begin() as session:
        session.add(
            ConversationRecord(
                id=conversation_id,
                user_id=user.id,
                title="movie",
                status="active",
                last_seq=1,
                last_turn_seq=1,
                created_at=occurred_utc,
                last_active_at=occurred_utc,
            )
        )
        session.add(
            MessageRecord(
                id=message_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                seq=1,
                role="user",
                content="我想看《星际穿越》。",
                privacy_level="L1",
                created_at=occurred_utc,
            )
        )
    timeline = TimelineStore(database)
    await timeline.index_message(
        user_id=user.id,
        conversation_id=conversation_id,
        message_id=message_id,
        actor=TimelineActor.USER,
        text="我想看《星际穿越》。",
        privacy_level=PrivacyLevel.L1,
        occurred_at=occurred_local,
    )
    recall = await HistoryRecallService(timeline).recall(
        "我昨天晚上说想看的电影是什么？",
        user_id=user.id,
        privacy_level=PrivacyLevel.L1,
        now=datetime(2026, 8, 19, 10, 0, tzinfo=timezone),
        timezone_name="Asia/Shanghai",
    )

    assert recall.mode is RecallMode.SOURCE
    assert recall.evidence
    assert recall.evidence[0].text == "我想看《星际穿越》。"
    assert recall.plan.start_at is not None
    assert recall.plan.start_at.date().isoformat() == "2026-08-18"


async def test_completed_turn_is_indexed_and_source_can_expand(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    requests: list[CompletionRequest] = []
    service, timeline = await _chat_service(database, tmp_path, requests)
    conversation = await service.create_conversation(user_id=user.id, title="food")

    await service.send_message(
        conversation.id,
        user_id=user.id,
        text="我今天午饭吃了番茄鸡蛋面。",
        privacy_level=PrivacyLevel.L1,
    )

    result = await timeline.search(
        user_id=user.id,
        query="番茄鸡蛋",
        source_types=[TimelineSourceType.MESSAGE],
    )
    assert result.events
    assert result.events[0].actor == TimelineActor.USER.value
    assert "番茄鸡蛋面" in result.events[0].summary

    evidence = await timeline.expand_sources(
        result.events,
        user_id=user.id,
        privacy_level=PrivacyLevel.L1,
    )
    assert evidence
    assert evidence[0].text == "我今天午饭吃了番茄鸡蛋面。"


async def test_l3_never_enters_timeline(
    database: Database, user: AppUserRecord
) -> None:
    timeline = TimelineStore(database)
    created = await timeline.index_message(
        user_id=user.id,
        conversation_id=uuid7(),
        message_id=uuid7(),
        actor=TimelineActor.USER,
        text="这段原始遥测/敏感内容不得持久化",
        privacy_level=PrivacyLevel.L3,
        occurred_at=datetime.now(UTC),
    )
    assert created is None
    result = await timeline.search(
        user_id=user.id,
        query="",
        privacy_levels=(PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2),
    )
    assert result.events == ()


async def test_history_recall_cross_conversation_injects_source_and_meta(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    requests: list[CompletionRequest] = []
    service, _ = await _chat_service(database, tmp_path, requests)
    first = await service.create_conversation(user_id=user.id, title="first")
    await service.send_message(
        first.id,
        user_id=user.id,
        text="我今天午饭吃了番茄鸡蛋面。",
        privacy_level=PrivacyLevel.L1,
    )

    second = await service.create_conversation(user_id=user.id, title="second")
    recalled = await service.send_message(
        second.id,
        user_id=user.id,
        text="刚才我说午饭吃了什么？",
        privacy_level=PrivacyLevel.L1,
    )

    system_prompt = requests[-1].messages[0].content
    assert "【历史回溯证据】" in system_prompt
    assert "番茄鸡蛋面" in system_prompt
    recall_meta = (recalled.assistant_message.decision_meta or {})["recall"]
    assert isinstance(recall_meta, dict)
    assert recall_meta["mode"] == "source"
    assert recall_meta["timeline_ids"]
    assert recall_meta["source_ids"]


async def test_timeline_only_mode_is_observable_when_source_is_unavailable(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    requests: list[CompletionRequest] = []
    service, timeline = await _chat_service(database, tmp_path, requests)
    now = datetime.now(UTC)
    await timeline.index_message(
        user_id=user.id,
        conversation_id=uuid7(),
        message_id=uuid7(),
        actor=TimelineActor.USER,
        text="刚才提到了一本叫《海边的卡夫卡》的书",
        privacy_level=PrivacyLevel.L1,
        occurred_at=now,
    )
    conversation = await service.create_conversation(user_id=user.id, title="timeline-only")

    turn = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="刚才提到的那本书叫什么？",
        privacy_level=PrivacyLevel.L1,
    )

    system_prompt = requests[-1].messages[0].content
    assert "【历史回溯证据】" in system_prompt
    assert "海边的卡夫卡" in system_prompt
    recall_meta = (turn.assistant_message.decision_meta or {})["recall"]
    assert isinstance(recall_meta, dict)
    assert recall_meta["mode"] == RecallMode.TIMELINE.value
    assert recall_meta["timeline_ids"]
    assert recall_meta["source_ids"] == []


async def test_history_recall_without_evidence_instructs_model_not_to_invent(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    requests: list[CompletionRequest] = []
    memory = MemoryStore(database)
    service, timeline = await _chat_service(
        database,
        tmp_path,
        requests,
        memory_store=memory,
    )
    await memory.add(
        MemoryCandidate(
            type=MemoryType.PREFERENCE,
            content="用户喜欢黑咖啡",
            privacy_level=PrivacyLevel.L1,
            fact_key="preference.drink",
            importance=0.8,
        ),
        user_id=user.id,
    )
    await memory.add(
        MemoryCandidate(
            type=MemoryType.COMMITMENT,
            content="双方约好周末一起看《星际穿越》",
            privacy_level=PrivacyLevel.L1,
            subject_kind=MemorySubjectKind.SHARED,
            subject_key="shared:user-assistant",
            importance=0.8,
        ),
        user_id=user.id,
    )
    zone = ZoneInfo(user.timezone)
    yesterday_evening = (datetime.now(zone) - timedelta(days=1)).replace(
        hour=21, minute=30, second=0, microsecond=0
    )
    await timeline.index_message(
        user_id=user.id,
        conversation_id=uuid7(),
        message_id=uuid7(),
        actor=TimelineActor.USER,
        text="我想看《银翼杀手2049》。",
        privacy_level=PrivacyLevel.L1,
        occurred_at=yesterday_evening,
    )
    conversation = await service.create_conversation(user_id=user.id, title="empty")

    turn = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="昨天我说过量子龙虾吗？",
        privacy_level=PrivacyLevel.L1,
    )

    system_prompt = requests[-1].messages[0].content
    assert "没有找到足以确认答案的证据" in system_prompt
    assert "黑咖啡" not in system_prompt
    assert "星际穿越" not in system_prompt
    recall_meta = (turn.assistant_message.decision_meta or {})["recall"]
    assert isinstance(recall_meta, dict)
    assert recall_meta["mode"] == RecallMode.NONE.value
    memory_meta = (turn.assistant_message.decision_meta or {})["memory"]
    assert isinstance(memory_meta, dict)
    hits = memory_meta["hits"]
    assert isinstance(hits, list)
    assert all(isinstance(hit, dict) and hit["grounded"] is False for hit in hits)


async def test_ordinary_chat_does_not_search_timeline(
    database: Database,
    user: AppUserRecord,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[CompletionRequest] = []
    service, timeline = await _chat_service(database, tmp_path, requests)
    original_search = timeline.search
    search_count = 0

    async def counted_search(*args: object, **kwargs: object) -> object:
        nonlocal search_count
        search_count += 1
        return await original_search(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(timeline, "search", counted_search)
    conversation = await service.create_conversation(user_id=user.id, title="ordinary")
    turn = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="今天心情还不错。",
        privacy_level=PrivacyLevel.L1,
    )

    assert search_count == 0
    recall_meta = (turn.assistant_message.decision_meta or {})["recall"]
    assert isinstance(recall_meta, dict)
    assert recall_meta["mode"] == RecallMode.WORKING.value


async def test_memory_hit_takes_precedence_when_timeline_has_no_evidence(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    config_path = tmp_path / "hub-memory.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    config_store = DatabaseConfigStore(database, config_path)
    await config_store.load()
    memory = MemoryStore(database)
    await memory.add(
        MemoryCandidate(
            type=MemoryType.PREFERENCE,
            content="用户不吃香菜",
            privacy_level=PrivacyLevel.L1,
            sources=[
                MemorySourceRef(
                    source_kind=MemorySourceKind.MANUAL,
                    source_id="timeline-memory-test",
                )
            ],
            importance=0.8,
        ),
        user_id=user.id,
    )
    timeline = TimelineStore(database)
    requests: list[CompletionRequest] = []
    service = ChatService(
        database,
        config_store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
        memory_store=memory,
        timeline_store=timeline,
        history_recall_service=HistoryRecallService(timeline),
    )
    conversation = await service.create_conversation(user_id=user.id, title="memory-first")

    turn = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="之前我是不是不吃香菜？",
        privacy_level=PrivacyLevel.L1,
    )

    system_prompt = requests[-1].messages[0].content
    assert "用户不吃香菜" in system_prompt
    assert "没有找到足以确认答案的证据" not in system_prompt
    recall_meta = (turn.assistant_message.decision_meta or {})["recall"]
    assert isinstance(recall_meta, dict)
    assert recall_meta["mode"] == RecallMode.MEMORY.value


async def test_l2_timeline_is_hidden_from_l1_but_expandable_in_l2(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    requests: list[CompletionRequest] = []
    service, timeline = await _chat_service(database, tmp_path, requests)
    first = await service.create_conversation(user_id=user.id, title="private")
    await service.send_message(
        first.id,
        user_id=user.id,
        text="我的秘密代号是青鸟。",
        privacy_level=PrivacyLevel.L2,
    )

    indexed = await timeline.search(
        user_id=user.id,
        query="",
        privacy_levels=(PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2),
    )
    assert indexed.events
    assert any(item.privacy_level == "L2" for item in indexed.events)
    assert all("青鸟" not in item.summary for item in indexed.events if item.privacy_level == "L2")

    public = await service.create_conversation(user_id=user.id, title="public")
    await service.send_message(
        public.id,
        user_id=user.id,
        text="刚才我的秘密代号是什么？",
        privacy_level=PrivacyLevel.L1,
    )
    assert "青鸟" not in requests[-1].messages[0].content

    private = await service.create_conversation(user_id=user.id, title="private-recall")
    await service.send_message(
        private.id,
        user_id=user.id,
        text="刚才我的秘密代号是什么？",
        privacy_level=PrivacyLevel.L2,
    )
    assert "青鸟" in requests[-1].messages[0].content


async def test_delete_conversation_removes_timeline_index(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    requests: list[CompletionRequest] = []
    service, timeline = await _chat_service(database, tmp_path, requests)
    conversation = await service.create_conversation(user_id=user.id, title="delete")
    await service.send_message(
        conversation.id,
        user_id=user.id,
        text="这是一条只用于删除测试的历史。",
        privacy_level=PrivacyLevel.L1,
    )
    before = await timeline.search(user_id=user.id, query="删除测试")
    assert before.events

    await service.delete_conversation(conversation.id, user_id=user.id)
    after = await timeline.search(user_id=user.id, query="删除测试")
    assert after.events == ()


async def test_event_bus_append_creates_timeline_index(database: Database) -> None:
    user_id = uuid7()
    event_id = uuid7()
    source = SourceRef(
        adapter_id="builtin.mock_input",
        adapter_instance_id=uuid7(),
        endpoint_id="timeline-test",
    )
    now = datetime.now(UTC)
    event = InputEnvelope(
        event_id=event_id,
        correlation_id=uuid7(),
        user_id=user_id,
        source=source,
        kind="device.temperature_changed",
        occurred_at=now,
        received_at=now,
        privacy_level="L1",
        content=[{"type": "text", "text": "客厅温度升到31度"}],
    )
    async with database.sessions.begin() as session:
        assert await append_event(session, event, topics=["events.timeline"])

    result = await TimelineStore(database).search(
        user_id=user_id,
        query="客厅温度",
        source_types=[TimelineSourceType.EVENT],
    )
    assert len(result.events) == 1
    assert result.events[0].source_id == str(event_id)
    assert result.events[0].actor == TimelineActor.DEVICE.value


async def test_admin_timeline_api_filters_and_returns_detail(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    config_store = DatabaseConfigStore(database, config_path)
    app = create_app(
        database,
        config_store=config_store,
        watch_config=False,
        admin_token="timeline-admin",
    )
    headers = {"Authorization": "Bearer timeline-admin"}
    timeline = TimelineStore(database)
    conversation_id = uuid4()
    message_id = uuid4()
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add(
            ConversationRecord(
                id=conversation_id,
                user_id=user.id,
                title="admin timeline",
                status="active",
                last_seq=1,
                last_turn_seq=1,
                created_at=now,
                last_active_at=now,
            )
        )
        session.add(
            MessageRecord(
                id=message_id,
                conversation_id=conversation_id,
                turn_id=uuid4(),
                seq=1,
                role="user",
                content="昨晚讨论了星际穿越",
                privacy_level="L1",
                created_at=now,
            )
        )
    created = await timeline.index_message(
        user_id=user.id,
        conversation_id=conversation_id,
        message_id=message_id,
        actor=TimelineActor.USER,
        text="昨晚讨论了星际穿越",
        privacy_level=PrivacyLevel.L1,
        occurred_at=now,
    )
    assert created is not None

    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthorized = await client.get(
            "/api/v1/admin/timeline", params={"user_id": str(user.id)}
        )
        queried = await client.post(
            "/api/v1/admin/timeline/query",
            headers=headers,
            json={"user_id": str(user.id), "query": "星际穿越", "limit": 10},
        )
        detail = await client.get(
            f"/api/v1/admin/timeline/{created.id}", headers=headers
        )
        source = await client.get(
            f"/api/v1/admin/timeline/{created.id}/source", headers=headers
        )

    assert unauthorized.status_code == 401
    assert queried.status_code == 200
    assert queried.json()["events"][0]["id"] == created.id
    assert detail.status_code == 200
    assert detail.json()["source_type"] == "message"
    assert source.status_code == 200
    assert source.json()["text"] == "昨晚讨论了星际穿越"
