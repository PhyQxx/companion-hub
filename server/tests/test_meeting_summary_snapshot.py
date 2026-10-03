"""Real DTO privacy and fallback evidence survive asynchronous waits."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_meetings import _config_yaml

from app.config import ConfigStore
from app.ids import uuid7
from app.llm.contracts import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.meetings.models import TranscriptSegment
from app.meetings.summarizer import LlmMeetingSummarizer
from app.schemas.common import PrivacyLevel


async def configuration(tmp_path: Path) -> ConfigStore:
    path = tmp_path / "meeting.yaml"
    path.write_text(_config_yaml())
    store = ConfigStore(path)
    await store.load()
    return store


@pytest.mark.parametrize("privacy", [PrivacyLevel.L2, PrivacyLevel.L3])
async def test_meeting_legacy_adapter_normalizes_real_request_privacy(
    privacy: PrivacyLevel, tmp_path: Path
) -> None:
    requests: list[CompletionRequest] = []

    class Backend:
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            requests.append(request)
            return CompletionResult(
                text='{"summary":"Synthetic summary"}',
                provider="fixture",
                model="fixture",
                endpoint="fixture",
                route=request.route,
                latency_ms=0,
            )

    store = await configuration(tmp_path)
    summarizer = LlmMeetingSummarizer(store, router_builder=lambda _: Backend())
    request = CompletionRequest(
        trace_id=uuid7(),
        messages=[LLMMessage(role="user", content="Synthetic")],
        privacy_level=privacy,
        route=LLMRoute.PRIVATE,
    )
    assert type(request.privacy_level) is str
    await summarizer.summarize(
        meeting_id=request.trace_id,
        title="Fixture",
        segments=[TranscriptSegment(speaker="Fixture", text="决定：保留原始证据")],
        privacy_level=request.privacy_level,
    )
    if privacy == PrivacyLevel.L3:
        assert requests == []
    else:
        assert len(requests) == 1 and requests[0].route == LLMRoute.PRIVATE


async def test_meeting_fallback_keeps_transcript_snapshot_from_before_wait(tmp_path: Path) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class Backend:
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            started.set()
            await release.wait()
            raise RuntimeError("synthetic completion failure")

    store = await configuration(tmp_path)
    summarizer = LlmMeetingSummarizer(store, router_builder=lambda _: Backend())
    segments = [TranscriptSegment(speaker="Original", text="决定：保留原始证据")]
    task = asyncio.create_task(
        summarizer.summarize(
            meeting_id=uuid7(), title="Fixture", segments=segments, privacy_level=PrivacyLevel.L1
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 3)
        segments[0] = segments[0].model_copy(
            update={"speaker": "Changed", "text": "决定：等待后替换证据"}
        )
        segments.append(TranscriptSegment(speaker="Injected", text="待办：后加任务"))
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.decisions[0].text == "保留原始证据"
        assert result.action_items == []
        assert "Original" in result.summary and "Changed" not in result.summary
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
