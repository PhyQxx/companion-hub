from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import create_meetings_router
from app.auth import AuthService
from app.calendar import CalendarParticipant, CalendarStore
from app.config import ConfigStore
from app.db import AppUserRecord, Base, Database, MeetingRecord, create_database
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult, LLMRoute
from app.meetings import (
    LlmMeetingSummarizer,
    MeetingActionItem,
    MeetingDecision,
    MeetingService,
    MeetingStore,
    MeetingSummary,
    TranscriptSegment,
)
from app.schemas import PrivacyLevel
from app.tasks import TaskStore

NOW = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)


class CapturingBackend:
    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        return CompletionResult(
            text=(
                '{"summary":"已形成发布决定。","decisions":[],"action_items":['
                '{"title":"完成回归","owner":"小王","due_at":null,'
                '"evidence":"行动项：完成回归","status":"created",'
                '"task_id":"01990bb8-7300-7000-8000-000000000001"}]}'
            ),
            provider="test",
            model="local-test",
            endpoint="local",
            route=request.route,
            latency_ms=1,
        )


def _config_yaml() -> str:
    return """
schema_version: 1
models:
  cloud:
    provider: openai_compatible
    model: cloud-test
    base_url: https://models.example/v1
    runs_local: false
    max_privacy_level: L1
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
  local:
    provider: openai_compatible
    model: local-test
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
        session.add(AppUserRecord(id=value, display_name="Meeting owner", status="active"))
    return value


class FakeSummarizer:
    async def summarize(self, **_: object) -> MeetingSummary:
        return MeetingSummary(
            summary="团队决定周五发布，发布前由小王完成回归。",
            decisions=[MeetingDecision(text="周五发布", evidence="决定：周五发布")],
            action_items=[
                MeetingActionItem(
                    title="完成发布回归",
                    owner="小王",
                    evidence="行动项：小王完成发布回归",
                )
            ],
        )


class PausedMeetingStore(MeetingStore):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.read_started = asyncio.Event()
        self.resume = asyncio.Event()
        self._pause_once = False

    def pause_next_read(self) -> None:
        self._pause_once = True

    async def _record(self, user_id: UUID, meeting_id: UUID) -> MeetingRecord:
        record = await super()._record(user_id, meeting_id)
        if self._pause_once:
            self._pause_once = False
            self.read_started.set()
            await self.resume.wait()
        return record


class PausedSummarizer(FakeSummarizer):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.resume = asyncio.Event()

    async def summarize(self, **kwargs: object) -> MeetingSummary:
        self.started.set()
        await self.resume.wait()
        return await super().summarize(**kwargs)


async def test_l2_summary_uses_private_route_and_cannot_create_tasks(tmp_path: Path) -> None:
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(_config_yaml(), encoding="utf-8")
    config_store = ConfigStore(config_path)
    await config_store.load()
    backend = CapturingBackend()
    summarizer = LlmMeetingSummarizer(
        config_store,
        router_builder=lambda _config: backend,
    )

    result = await summarizer.summarize(
        meeting_id=uuid7(),
        title="隐私评审",
        segments=[TranscriptSegment(speaker="小王", text="行动项：完成回归")],
        privacy_level=PrivacyLevel.L2,
    )

    assert backend.requests[0].route == LLMRoute.PRIVATE
    assert backend.requests[0].privacy_level == PrivacyLevel.L2
    assert backend.requests[0].json_mode is True
    assert result.action_items[0].status == "proposed"
    assert result.action_items[0].task_id is None


def _service(database: Database) -> MeetingService:
    return MeetingService(
        MeetingStore(database),
        CalendarStore(database),
        TaskStore(database),
        FakeSummarizer(),
        clock=lambda: NOW,
    )


async def test_prepare_from_calendar_builds_briefing_without_authorizing(
    database: Database, user_id: UUID
) -> None:
    calendar = CalendarStore(database)
    event = await calendar.create_event(
        user_id=user_id,
        title="产品评审",
        starts_at=NOW + timedelta(hours=1),
        ends_at=NOW + timedelta(hours=2),
        location="会议室 A",
        notes="评审发布范围",
        participants=[CalendarParticipant(name="小王")],
        now=NOW,
    )

    meeting = await _service(database).prepare(
        user_id,
        title=None,
        participants=[],
        privacy_level=PrivacyLevel.L1,
        calendar_event_id=event.id,
    )

    assert meeting.status == "prepared"
    assert meeting.consent_at is None
    assert meeting.title == "产品评审"
    assert meeting.participants == ["小王"]
    assert meeting.briefing["location"] == "会议室 A"
    assert meeting.transcript_segments == []


async def test_transcript_requires_consent_and_declared_speaker(
    database: Database, user_id: UUID
) -> None:
    service = _service(database)
    meeting = await service.prepare(
        user_id,
        title="周会",
        participants=["小王"],
        privacy_level=PrivacyLevel.L1,
    )
    segment = TranscriptSegment(speaker="小王", text="决定：周五发布")

    with pytest.raises(PermissionError, match="authorization"):
        await service.append_transcript(user_id, meeting.id, [segment])
    await service.authorize_transcription(user_id, meeting.id)
    with pytest.raises(ValueError, match="declared participant"):
        await service.append_transcript(
            user_id,
            meeting.id,
            [TranscriptSegment(speaker="未知来宾", text="我同意")],
        )
    updated = await service.append_transcript(user_id, meeting.id, [segment])
    assert updated.transcript_segments == [segment]


async def test_revoke_stops_future_transcript(database: Database, user_id: UUID) -> None:
    service = _service(database)
    meeting = await service.prepare(
        user_id,
        title="访谈",
        participants=["小王"],
        privacy_level=PrivacyLevel.L2,
    )
    await service.authorize_transcription(user_id, meeting.id)
    revoked = await service.revoke_transcription(user_id, meeting.id)
    assert revoked.status == "consent_revoked"
    assert revoked.consent_revoked_at == NOW
    with pytest.raises(PermissionError):
        await service.append_transcript(
            user_id,
            meeting.id,
            [TranscriptSegment(speaker="小王", text="不应保存")],
        )


async def test_revoke_wins_against_an_inflight_transcript_batch(
    database: Database, user_id: UUID
) -> None:
    paused_store = PausedMeetingStore(database)
    service = MeetingService(
        paused_store,
        CalendarStore(database),
        TaskStore(database),
        FakeSummarizer(),
        clock=lambda: NOW,
    )
    meeting = await service.prepare(
        user_id,
        title="撤销竞态",
        participants=["小王"],
        privacy_level=PrivacyLevel.L1,
    )
    await service.authorize_transcription(user_id, meeting.id)
    paused_store.pause_next_read()
    append_task = asyncio.create_task(
        service.append_transcript(
            user_id,
            meeting.id,
            [TranscriptSegment(speaker="小王", text="不应在撤销后写入")],
        )
    )
    await paused_store.read_started.wait()
    await MeetingStore(database).revoke(user_id, meeting.id, NOW)
    paused_store.resume.set()

    with pytest.raises(PermissionError, match="authorization"):
        await append_task
    final = await service.get(user_id, meeting.id)
    assert final.status == "consent_revoked"
    assert final.transcript_segments == []


async def test_finish_proposes_actions_and_confirmation_creates_task_once(
    database: Database, user_id: UUID
) -> None:
    service = _service(database)
    meeting = await service.prepare(
        user_id,
        title="发布会",
        participants=["小王"],
        privacy_level=PrivacyLevel.L1,
    )
    await service.authorize_transcription(user_id, meeting.id)
    await service.append_transcript(
        user_id,
        meeting.id,
        [
            TranscriptSegment(speaker="我", text="决定：周五发布"),
            TranscriptSegment(speaker="小王", text="行动项：完成发布回归"),
        ],
    )
    completed = await service.finish(user_id, meeting.id)

    assert completed.status == "completed"
    assert completed.decisions[0].text == "周五发布"
    assert completed.action_items[0].status == "proposed"
    assert await TaskStore(database).list_tasks(user_id) == []
    with pytest.raises(ValueError, match="due time"):
        await service.confirm_action_item(user_id, meeting.id, 0)

    due_at = NOW + timedelta(days=2)
    confirmed = await service.confirm_action_item(user_id, meeting.id, 0, due_at=due_at)
    replay = await service.confirm_action_item(user_id, meeting.id, 0, due_at=due_at)
    tasks = await TaskStore(database).list_tasks(user_id)
    assert len(tasks) == 1
    assert tasks[0].title == "完成发布回归"
    assert confirmed.action_items[0].task_id == tasks[0].id
    assert replay.action_items[0].task_id == tasks[0].id


async def test_finish_rejects_a_summary_when_transcript_changes_during_generation(
    database: Database, user_id: UUID
) -> None:
    summarizer = PausedSummarizer()
    service = MeetingService(
        MeetingStore(database),
        CalendarStore(database),
        TaskStore(database),
        summarizer,
        clock=lambda: NOW,
    )
    meeting = await service.prepare(
        user_id,
        title="摘要竞态",
        participants=["小王"],
        privacy_level=PrivacyLevel.L1,
    )
    await service.authorize_transcription(user_id, meeting.id)
    await service.append_transcript(
        user_id,
        meeting.id,
        [TranscriptSegment(speaker="小王", text="第一段")],
    )
    finish_task = asyncio.create_task(service.finish(user_id, meeting.id))
    await summarizer.started.wait()
    await service.append_transcript(
        user_id,
        meeting.id,
        [TranscriptSegment(speaker="小王", text="生成摘要期间新增的第二段")],
    )
    summarizer.resume.set()

    with pytest.raises(RuntimeError, match="transcript changed"):
        await finish_task
    current = await service.get(user_id, meeting.id)
    assert current.status == "recording"
    assert len(current.transcript_segments) == 2


async def test_meeting_api_requires_user_auth_and_explicit_true_consent(
    database: Database,
) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="Meeting API", password="correct horse")
    app = FastAPI()
    app.include_router(create_meetings_router(_service(database), auth))
    headers = {"Authorization": f"Bearer {owner.access_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/api/v1/meetings")
        prepared = await client.post(
            "/api/v1/meetings",
            headers=headers,
            json={"title": "接口会议", "participants": ["小王"]},
        )
        meeting_id = prepared.json()["id"]
        rejected = await client.post(
            f"/api/v1/meetings/{meeting_id}/transcription/authorize",
            headers=headers,
            json={"authorized": False},
        )
        accepted = await client.post(
            f"/api/v1/meetings/{meeting_id}/transcription/authorize",
            headers=headers,
            json={"authorized": True},
        )

    assert unauthorized.status_code == 401
    assert prepared.status_code == 201
    assert rejected.status_code == 422
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "recording"


async def test_concurrent_action_confirmation_never_duplicates_task(
    tmp_path: Path,
) -> None:
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'meeting.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user_id, display_name="Concurrent", status="active"))
    first = _service(database)
    second = _service(database)
    meeting = await first.prepare(
        user_id,
        title="并发确认",
        participants=["小王"],
        privacy_level=PrivacyLevel.L1,
    )
    await first.authorize_transcription(user_id, meeting.id)
    await first.append_transcript(
        user_id,
        meeting.id,
        [TranscriptSegment(speaker="小王", text="行动项：完成并发验证")],
    )
    await first.finish(user_id, meeting.id)
    due_at = NOW + timedelta(days=1)

    results = await asyncio.gather(
        first.confirm_action_item(user_id, meeting.id, 0, due_at=due_at),
        second.confirm_action_item(user_id, meeting.id, 0, due_at=due_at),
        return_exceptions=True,
    )

    tasks = await TaskStore(database).list_tasks(user_id)
    try:
        assert len(tasks) == 1, results
        assert any(not isinstance(result, Exception) for result in results)
        final = await first.get(user_id, meeting.id)
        assert final.action_items[0].task_id == tasks[0].id
    finally:
        await database.close()
