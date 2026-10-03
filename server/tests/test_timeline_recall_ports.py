"""History and activity policies own their nested snapshots and query ports."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from test_timeline_recall_privacy import NOW

from app.ids import uuid7
from app.schemas import PrivacyLevel
from app.timeline.browser_recall import BrowserActivityRecallService
from app.timeline.models import TimelineEvent, TimelineEvidence, TimelineSearchResult
from app.timeline.recall import HistoryRecallService
from app.timeline.screen_recall import ScreenActivityRecallService
from scripts.check_architecture import allowed


def event(owner: UUID) -> TimelineEvent:
    return TimelineEvent(
        id=1,
        user_id=owner,
        occurred_at=NOW - timedelta(minutes=20),
        ended_at=None,
        source_type="message",
        source_id=str(uuid7()),
        actor="user",
        event_type="conversation.message",
        conversation_id=uuid7(),
        title="Fixture",
        summary="Synthetic observation",
        privacy_level="L1",
        importance=0.2,
        entities=({"value": "original"},),
        keywords=(),
        metadata={"value": "original"},
        created_at=NOW,
    )


class Repository:
    def __init__(self, owner: UUID) -> None:
        self.result = TimelineSearchResult((event(owner),), 1)
        self.calls: list[dict[str, Any]] = []
        self.expanded: tuple[TimelineEvent, ...] = ()
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.wait = False
        self.change_expansion = False

    async def search(self, **kwargs: Any) -> TimelineSearchResult:
        self.calls.append(kwargs)
        return self.result

    async def expand_sources(
        self,
        events: Sequence[TimelineEvent],
        *,
        user_id: UUID,
        privacy_level: PrivacyLevel,
        limit: int = 8,
    ) -> tuple[TimelineEvidence, ...]:
        self.expanded = tuple(events)
        if self.change_expansion:
            self.expanded[0].metadata.clear()
            self.expanded[0].entities[0].clear()
        self.entered.set()
        if self.wait:
            await self.release.wait()
        return ()


@pytest.mark.parametrize("change", ["repository", "expansion"])
async def test_history_result_owns_nested_evidence_before_source_wait(change: str) -> None:
    owner = uuid7()
    repository = Repository(owner)
    repository.wait = True
    repository.change_expansion = change == "expansion"
    task = asyncio.create_task(
        HistoryRecallService(repository).recall(
            "刚才我提到合成证据", user_id=owner, privacy_level=PrivacyLevel.L1, now=NOW
        )
    )
    try:
        await asyncio.wait_for(repository.entered.wait(), 3)
        if change == "repository":
            repository.result.events[0].metadata.clear()
            repository.result.events[0].entities[0].clear()
        repository.release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.events[0].metadata == {"value": "original"}
        assert result.events[0].entities == ({"value": "original"},)
    finally:
        repository.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("service", ["screen", "browser"])
async def test_activity_result_does_not_share_metadata_with_repository(service: str) -> None:
    owner = uuid7()
    repository = Repository(owner)
    repository.result = TimelineSearchResult(
        (
            replace(
                repository.result.events[0],
                source_type="device",
                event_type=f"{service}.observed",
                actor="device",
            ),
        ),
        1,
    )
    if service == "screen":
        screen = await ScreenActivityRecallService(repository).recall(
            "总结最近一小时电脑活动",
            user_id=owner,
            privacy_level=PrivacyLevel.L1,
            now=NOW,
            timezone_name="UTC",
        )
        assert screen is not None
        events = screen.events
    else:
        browser = await BrowserActivityRecallService(repository).recall(
            "总结最近一小时浏览的网站",
            user_id=owner,
            privacy_level=PrivacyLevel.L1,
            now=NOW,
            timezone_name="UTC",
        )
        assert browser is not None
        events = browser.events
    events[0].metadata.clear()
    events[0].entities[0].clear()
    assert repository.result.events[0].metadata == {"value": "original"}
    assert repository.result.events[0].entities == ({"value": "original"},)


@pytest.mark.parametrize("service", ["history", "screen", "browser"])
@pytest.mark.parametrize("error", [RuntimeError("fixture"), asyncio.CancelledError("fixture")])
async def test_recall_query_errors_and_cancellation_propagate_once(
    service: str,
    error: BaseException,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid7()
    repository = Repository(owner)
    calls = 0

    async def rejected(**kwargs: Any) -> TimelineSearchResult:
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(repository, "search", rejected)
    with pytest.raises(type(error)) as caught:
        if service == "history":
            await HistoryRecallService(repository).recall(
                "刚才我提到合成证据", user_id=owner, privacy_level=PrivacyLevel.L1, now=NOW
            )
        elif service == "screen":
            await ScreenActivityRecallService(repository).recall(
                "总结最近一小时电脑活动",
                user_id=owner,
                privacy_level=PrivacyLevel.L1,
                now=NOW,
                timezone_name="UTC",
            )
        else:
            await BrowserActivityRecallService(repository).recall(
                "总结最近一小时浏览的网站",
                user_id=owner,
                privacy_level=PrivacyLevel.L1,
                now=NOW,
                timezone_name="UTC",
            )
    assert caught.value is error and calls == 1


@pytest.mark.parametrize("service", ["screen", "browser"])
@pytest.mark.parametrize("time_intent", [False, True])
async def test_activity_without_intent_or_time_does_not_query(
    service: str, time_intent: bool
) -> None:
    owner = uuid7()
    repository = Repository(owner)
    query = "今天聊点什么" if time_intent else "总结我的电脑活动"
    if service == "screen":
        assert (
            await ScreenActivityRecallService(repository).recall(
                query, user_id=owner, privacy_level=PrivacyLevel.L1, now=NOW, timezone_name="UTC"
            )
            is None
        )
    else:
        query = "今天聊点什么" if time_intent else "总结我浏览的网站"
        assert (
            await BrowserActivityRecallService(repository).recall(
                query, user_id=owner, privacy_level=PrivacyLevel.L1, now=NOW, timezone_name="UTC"
            )
            is None
        )
    assert repository.calls == []


async def test_history_broad_expansion_is_bounded_to_two_searches() -> None:
    owner = uuid7()
    repository = Repository(owner)
    repository.result = TimelineSearchResult((), 0)
    result = await HistoryRecallService(repository).recall(
        "以前我说过什么", user_id=owner, privacy_level=PrivacyLevel.L1, now=NOW, timezone_name="UTC"
    )
    assert result.search_count == len(repository.calls) == 2
    assert repository.calls[0]["start_at"] == NOW - timedelta(days=30)
    assert repository.calls[1]["start_at"] == NOW - timedelta(days=90)
    assert all(call["limit"] == 8 and call["user_id"] == owner for call in repository.calls)


async def test_l2_time_sources_precede_general_results_without_duplicate_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid7()
    repository = Repository(owner)
    public = event(owner)
    private = replace(public, id=2, privacy_level="L2", summary="L2 message")

    async def search(**kwargs: Any) -> TimelineSearchResult:
        repository.calls.append(kwargs)
        return TimelineSearchResult((private, public) if kwargs["query"] == "" else (public,), 2)

    monkeypatch.setattr(repository, "search", search)
    result = await HistoryRecallService(repository).recall(
        "刚才我提到合成证据",
        user_id=owner,
        privacy_level=PrivacyLevel.L2,
        now=NOW,
        timezone_name="UTC",
    )
    assert [item.id for item in result.events] == [2, 1]
    assert [item.id for item in repository.expanded] == [2, 1]
    assert repository.calls[1]["privacy_levels"] == (PrivacyLevel.L2,)
    assert repository.calls[1]["limit"] == 6 and result.search_count == 2


@pytest.mark.parametrize(
    "module",
    [
        "app.timeline.ports",
        "app.timeline.recall",
        "app.timeline.screen_recall",
        "app.timeline.browser_recall",
    ],
)
@pytest.mark.parametrize(
    "adapter", ["sqlalchemy", "httpx", "app.timeline.store", "app.llm.provider", "app.main"]
)
def test_timeline_recall_rejects_concrete_adapters(module: str, adapter: str) -> None:
    assert not allowed(module, adapter)
