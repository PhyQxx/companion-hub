"""Meeting execution adapters preserve owned budget context on every exit."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from test_meeting_coordinator_boundary import Repository, Summarizer, service

from app.meetings.models import ActionClaim, MeetingSummary, TranscriptSegment
from app.meetings.service import _OwnedSummarizer
from app.meetings.store import ActionClaim as LegacyClaim
from app.runs.completion import current_model_owner, model_owner
from app.schemas.common import PrivacyLevel
from scripts.check_architecture import allowed


async def test_summary_port_receives_explicit_owner_and_fixed_transcript() -> None:
    repository = Repository()
    summarizer = Summarizer(repository)
    await service(repository, summarizer).finish(repository.owner, repository.id)
    (request,) = summarizer.calls
    assert request["user_id"] == repository.owner
    assert request["meeting_id"] == repository.id
    assert request["privacy_level"] == "L1"
    assert repository.completed_count == 1


@pytest.mark.parametrize("exit_kind", ["success", "error", "cancel"])
async def test_legacy_model_owner_scope_is_restored_on_every_exit(exit_kind: str) -> None:
    owner, outer = uuid4(), uuid4()
    seen: list[object] = []

    class LegacySummary:
        async def summarize(self, **kwargs: Any) -> MeetingSummary:
            seen.append(current_model_owner())
            assert "user_id" not in kwargs
            if exit_kind == "error":
                raise RuntimeError("synthetic summary failure")
            if exit_kind == "cancel":
                raise asyncio.CancelledError
            return MeetingSummary(summary="Synthetic summary")

    with model_owner(outer):
        adapter = _OwnedSummarizer(LegacySummary())

        async def invoke() -> MeetingSummary:
            return await adapter.summarize(
                user_id=owner,
                meeting_id=uuid4(),
                title="Fixture",
                segments=[TranscriptSegment(speaker="self", text="Synthetic")],
                privacy_level=PrivacyLevel.L1,
            )

        if exit_kind == "success":
            result = await invoke()
            assert result.summary == "Synthetic summary"
        else:
            with pytest.raises(RuntimeError if exit_kind == "error" else asyncio.CancelledError):
                await invoke()
        assert current_model_owner() == outer
    assert seen == [owner]
    assert current_model_owner() is None


@pytest.mark.parametrize("cancel", [False, True])
async def test_summary_failure_does_not_complete_or_retry(cancel: bool) -> None:
    repository = Repository()

    class Broken(Summarizer):
        async def summarize(self, **kwargs: Any) -> MeetingSummary:
            self.calls.append(kwargs)
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("synthetic summary failure")

    summarizer = Broken(repository)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await service(repository, summarizer).finish(repository.owner, repository.id)
    assert len(summarizer.calls) == 1
    assert repository.completed_count is None
    assert repository.meeting.status == "recording"


async def test_owned_read_snapshot_does_not_share_nested_meeting_input() -> None:
    repository = Repository()
    view = await service(repository, Summarizer(repository)).get(repository.owner, repository.id)
    repository.meeting.transcript_segments.clear()
    repository.meeting.participants.clear()
    repository.meeting.briefing["changed"] = True
    assert len(view.transcript_segments) == 1 and view.participants == ["self"]
    assert view.briefing == {}


def test_action_claim_alias_keeps_existing_contract() -> None:
    assert LegacyClaim is ActionClaim
    claim = ActionClaim(True, "creating", None)
    assert claim.created and claim.status == "creating" and claim.task_id is None


@pytest.mark.parametrize("module", ["app.meetings.service_core", "app.meetings.service_ports"])
@pytest.mark.parametrize(
    "dependency", ["app.db", "app.meetings.store", "sqlalchemy", "httpx", "openai"]
)
def test_meeting_coordinator_static_boundaries(module: str, dependency: str) -> None:
    assert not allowed(module, dependency)
