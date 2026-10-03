"""Recall adapters honor real string privacy and malformed time inputs."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from test_timeline import database as timeline_database
from test_timeline import user as timeline_user

from app.db import AppUserRecord, ConversationRecord, Database, MessageRecord
from app.ids import uuid7
from app.llm.contracts import CompletionRequest, LLMMessage, LLMRoute
from app.schemas.common import PrivacyLevel
from app.timeline import (
    BrowserActivityRecallService,
    HistoryRecallService,
    ScreenActivityRecallService,
)
from app.timeline.models import (
    HistoryRecallResult,
    TimelineActor,
    TimelineEvidence,
    TimelineSearchResult,
)
from app.timeline.recall import TemporalQueryParser
from app.timeline.store import TimelineStore

database = timeline_database
user = timeline_user
NOW = datetime(2030, 1, 10, 9, tzinfo=UTC)


class Repository:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> TimelineSearchResult:
        self.calls.append(kwargs)
        return TimelineSearchResult(events=(), candidate_count=0)

    async def expand_sources(self, *args: Any, **kwargs: Any) -> tuple[TimelineEvidence, ...]:
        raise AssertionError("Empty search must not expand sources")


@pytest.mark.parametrize("service", ["history", "screen", "browser"])
@pytest.mark.parametrize("privacy", [PrivacyLevel.L0, PrivacyLevel.L2, PrivacyLevel.L3])
async def test_recall_normalizes_actual_request_privacy_and_private_calls_are_zero(
    service: str,
    privacy: PrivacyLevel,
) -> None:
    request = CompletionRequest(
        trace_id=uuid7(),
        messages=[LLMMessage(role="user", content="Synthetic")],
        privacy_level=privacy,
        route=LLMRoute.PRIVATE,
    )
    assert type(request.privacy_level) is str
    repository = Repository()
    if service == "history":
        history = await HistoryRecallService(repository).recall(
            "昨天我提到合成证据",
            user_id=request.trace_id,
            privacy_level=request.privacy_level,
            now=NOW,
            timezone_name="UTC",
        )
    elif service == "screen":
        screen = await ScreenActivityRecallService(repository).recall(
            "总结最近一小时在电脑上做了什么",
            user_id=request.trace_id,
            privacy_level=request.privacy_level,
            now=NOW,
            timezone_name="UTC",
        )
    else:
        browser = await BrowserActivityRecallService(repository).recall(
            "总结最近一小时看了什么网站",
            user_id=request.trace_id,
            privacy_level=request.privacy_level,
            now=NOW,
            timezone_name="UTC",
        )
    if privacy is PrivacyLevel.L3:
        assert repository.calls == []
        if service == "history":
            assert isinstance(history, HistoryRecallResult) and history.search_count == 0
        else:
            assert (screen if service == "screen" else browser) is None
    else:
        expected = (PrivacyLevel.L0,) if privacy is PrivacyLevel.L0 else tuple(PrivacyLevel)[:3]
        assert repository.calls[0]["privacy_levels"] == expected
        assert len(repository.calls) == (
            2 if service == "history" and privacy is PrivacyLevel.L2 else 1
        )


@pytest.mark.parametrize(
    "privacy", [PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2, PrivacyLevel.L3]
)
async def test_source_expansion_uses_actual_privacy_scope(
    database: Database, user: AppUserRecord, privacy: PrivacyLevel
) -> None:
    request = CompletionRequest(
        trace_id=uuid7(),
        messages=[LLMMessage(role="user", content="Synthetic")],
        privacy_level=privacy,
        route=LLMRoute.PRIVATE,
    )
    source_privacy = PrivacyLevel.L2 if privacy is PrivacyLevel.L2 else PrivacyLevel.L1
    conversation, message = uuid7(), uuid7()
    async with database.sessions.begin() as session:
        session.add(ConversationRecord(id=conversation, user_id=user.id, title="Fixture"))
        await session.flush()
        session.add(
            MessageRecord(
                id=message,
                conversation_id=conversation,
                turn_id=uuid7(),
                seq=1,
                role="user",
                content="Synthetic source",
                privacy_level=source_privacy.value,
            )
        )
    store = TimelineStore(database)
    event = await store.index_message(
        user_id=user.id,
        conversation_id=conversation,
        message_id=message,
        actor=TimelineActor.USER,
        text="Synthetic source",
        privacy_level=source_privacy,
        occurred_at=NOW,
    )
    assert event is not None
    if privacy is PrivacyLevel.L1:
        event = replace(event, user_id=uuid7())
    evidence = await store.expand_sources(
        (event,), user_id=user.id, privacy_level=request.privacy_level
    )
    assert len(evidence) == (1 if privacy is PrivacyLevel.L2 else 0)


@pytest.mark.parametrize("amount", ["十十", "九九", "一二", "0"])
def test_malformed_recent_amount_does_not_break_recall(amount: str) -> None:
    assert TemporalQueryParser("UTC").parse(f"过去{amount}分钟的电脑活动", now=NOW) is None


@pytest.mark.parametrize("timezone", ["", "/UTC", "../UTC"])
def test_invalid_timezone_uses_existing_fallback(timezone: str) -> None:
    assert TemporalQueryParser(timezone).timezone_name == "Asia/Shanghai"
