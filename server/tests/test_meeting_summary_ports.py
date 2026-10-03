"""Detached meeting policy retains validation, fallback and legacy request fields."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from uuid import UUID

import pytest
from test_meeting_summary_snapshot import configuration

from app.harness.budget import BudgetDenied
from app.ids import uuid7
from app.llm.contracts import CompletionRequest, CompletionResult, LLMRoute
from app.meetings.models import MeetingSummary, TranscriptSegment
from app.meetings.summarizer import LlmMeetingSummarizer
from app.meetings.summary_core import (
    MAX_SUMMARY_INPUT_CHARS,
    MEETING_SUMMARY_INSTRUCTION,
    RuleBasedMeetingSummarizer,
    StructuredMeetingSummarizer,
)
from app.meetings.summary_ports import MeetingSummaryCompletion, MeetingSummaryInput
from app.meetings.summary_sql import SqlMeetingSummaryCompletion
from app.schemas.common import PrivacyLevel
from scripts.check_architecture import allowed

VALID = json.dumps(
    {
        "summary": "Synthetic summary",
        "decisions": [],
        "action_items": [
            {
                "title": "Synthetic task",
                "owner": "Fixture",
                "status": "created",
                "task_id": str(uuid7()),
            }
        ],
    }
)


class Completion:
    def __init__(self, text: str = VALID, error: BaseException | None = None) -> None:
        self.text, self.error = text, error
        self.inputs: list[MeetingSummaryInput] = []

    async def complete(self, request: MeetingSummaryInput) -> MeetingSummaryCompletion:
        self.inputs.append(request)
        if self.error is not None:
            raise self.error
        return MeetingSummaryCompletion(self.text)


async def summarized(
    completion: Completion, privacy: PrivacyLevel = PrivacyLevel.L1
) -> MeetingSummary:
    return await StructuredMeetingSummarizer(completion).summarize(
        meeting_id=uuid7(),
        title="Fixture",
        segments=[TranscriptSegment(speaker="Fixture", text="决定：保留原始证据")],
        privacy_level=privacy,
    )


@pytest.mark.parametrize("privacy", [PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2])
async def test_detached_meeting_completion_preserves_privacy_and_proposals(
    privacy: PrivacyLevel,
) -> None:
    completion = Completion()
    result = await summarized(completion, privacy)
    assert result.summary == "Synthetic summary"
    assert result.action_items[0].status == "proposed" and result.action_items[0].task_id is None
    assert completion.inputs[0].privacy_level is privacy
    assert completion.inputs[0].instruction == MEETING_SUMMARY_INSTRUCTION
    assert completion.inputs[0].text == "会议：Fixture\n转写：\nFixture：决定：保留原始证据"


async def test_private_meeting_uses_offline_fallback_without_completion() -> None:
    completion = Completion()
    result = await summarized(completion, PrivacyLevel.L3)
    assert completion.inputs == []
    assert result.decisions[0].text == "保留原始证据"


@pytest.mark.parametrize(
    "bad",
    [
        "bad",
        "[]",
        "null",
        "{}",
        '{"summary":""}',
        '{"summary":"ok","action_items":[{"title":"task","status":"done"}]}',
        '{"summary":"ok","decisions":[{"text":""}]}',
    ],
)
async def test_invalid_meeting_output_keeps_rule_fallback(bad: str) -> None:
    result = await summarized(Completion(bad))
    assert result.decisions[0].text == "保留原始证据" and result.action_items == []


@pytest.mark.parametrize(
    "reason",
    [
        "budget_run_inactive",
        "run_source_not_found",
        "model_run_owner_missing",
        "budget_owner_invalid",
    ],
)
async def test_terminal_meeting_rejection_does_not_fallback(reason: str) -> None:
    with pytest.raises(BudgetDenied, match=reason):
        await summarized(Completion(error=BudgetDenied(reason)))


@pytest.mark.parametrize(
    "error", [RuntimeError("synthetic failure"), BudgetDenied("run_budget_exhausted")]
)
async def test_nonterminal_meeting_failure_keeps_original_fallback(error: Exception) -> None:
    result = await summarized(Completion(error=error))
    assert result.decisions[0].text == "保留原始证据"


async def test_cancelled_meeting_completion_propagates() -> None:
    with pytest.raises(asyncio.CancelledError):
        await summarized(Completion(error=asyncio.CancelledError()))


@pytest.mark.parametrize(
    "framing", ["{}", "```json\n{}\n```", "synthetic prefix\n{}\nsynthetic suffix"]
)
async def test_meeting_core_keeps_framed_object_parsing(framing: str) -> None:
    result = await summarized(Completion(framing.format(VALID)))
    assert result.summary == "Synthetic summary"


async def test_meeting_completion_input_is_bounded_including_title() -> None:
    completion = Completion()
    segments = [TranscriptSegment(speaker="Fixture", text="x" * 4000) for _ in range(30)]
    await StructuredMeetingSummarizer(completion).summarize(
        meeting_id=uuid7(), title="Fixture", segments=segments, privacy_level=PrivacyLevel.L1
    )
    assert len(completion.inputs[0].text) == MAX_SUMMARY_INPUT_CHARS
    assert completion.inputs[0].text.startswith("会议：Fixture\n转写：\n")


def test_detached_meeting_dtos_are_frozen() -> None:
    def overwrite(value: object, attribute: str) -> None:
        setattr(value, attribute, "changed")

    request = MeetingSummaryInput(uuid7(), "instruction", "text", PrivacyLevel.L1)
    with pytest.raises(FrozenInstanceError):
        overwrite(request, "text")
    with pytest.raises(FrozenInstanceError):
        overwrite(MeetingSummaryCompletion("text"), "text")


@pytest.mark.parametrize("privacy", [PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2])
async def test_legacy_meeting_adapter_retains_model_request(
    privacy: PrivacyLevel, tmp_path: Path
) -> None:
    requests: list[CompletionRequest] = []

    class Backend:
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            requests.append(request)
            return CompletionResult(
                text=VALID,
                provider="fixture",
                model="fixture",
                endpoint="fixture",
                route=request.route,
                latency_ms=0,
            )

    store = await configuration(tmp_path)
    identifier: UUID = uuid7()
    result = await LlmMeetingSummarizer(store, router_builder=lambda _: Backend()).summarize(
        meeting_id=identifier,
        title="Fixture",
        segments=[TranscriptSegment(speaker="Fixture", text="Original")],
        privacy_level=privacy,
    )
    assert result.summary == "Synthetic summary"
    request = requests[0]
    assert request.trace_id == identifier and request.privacy_level == privacy
    assert request.route == (LLMRoute.PRIVATE if privacy is PrivacyLevel.L2 else LLMRoute.UTILITY)
    assert request.max_tokens == 4096 and request.temperature == 0 and request.json_mode
    assert len(request.messages) == 2 and request.messages[0].content == MEETING_SUMMARY_INSTRUCTION
    assert request.messages[1].content == "会议：Fixture\n转写：\nFixture：Original"


async def test_direct_sql_meeting_adapter_rejects_private_before_config_refresh(
    tmp_path: Path,
) -> None:
    builds = 0

    def forbidden(config: object) -> Backend:
        nonlocal builds
        builds += 1
        raise AssertionError("Private input reached router construction")

    class Backend:
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            raise AssertionError("Private input reached completion")

    # An unloaded store proves the gate runs before accessing current config.
    from app.config import ConfigStore

    port = SqlMeetingSummaryCompletion(
        ConfigStore(tmp_path / "unloaded.yaml"), router_builder=forbidden
    )
    with pytest.raises(BudgetDenied, match="l3_model_forbidden"):
        await port.complete(MeetingSummaryInput(uuid7(), "instruction", "text", PrivacyLevel.L3))
    assert builds == 0


def test_rule_meeting_exports_remain_identical() -> None:
    from app.meetings import RuleBasedMeetingSummarizer as package_rule
    from app.meetings.summarizer import RuleBasedMeetingSummarizer as legacy_rule

    assert package_rule is legacy_rule is RuleBasedMeetingSummarizer


@pytest.mark.parametrize(
    "module", ["app.meetings.summary_core", "app.meetings.summary_ports", "app.meetings.models"]
)
@pytest.mark.parametrize(
    "adapter", ["app.db", "app.llm", "app.memory.extraction", "sqlalchemy", "httpx"]
)
def test_meeting_policy_cannot_import_runtime_adapters(module: str, adapter: str) -> None:
    assert not allowed(module, adapter)
