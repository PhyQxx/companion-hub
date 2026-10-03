"""Meeting summary policy and offline rules, independent of persistence/providers."""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from typing import Protocol
from uuid import UUID

from app.harness.budget import BudgetDenied
from app.harness.json_payload import load_json_object
from app.schemas.common import PrivacyLevel

from .models import MeetingActionItem, MeetingDecision, MeetingSummary, TranscriptSegment
from .summary_ports import MeetingSummaryCompletionPort, MeetingSummaryInput

logger = logging.getLogger("app.meetings.summarizer")
MAX_SUMMARY_INPUT_CHARS = 100_000

MEETING_SUMMARY_INSTRUCTION = (
    "你是会议纪要整理器。只依据转写内容输出严格 JSON："
    '{"summary":"摘要","decisions":[{"text":"决定",'
    '"evidence":"原文证据"}],"action_items":[{"title":"任务",'
    '"owner":"负责人或null","due_at":"ISO时间或null",'
    '"evidence":"原文证据"}]}。不得补造决定、负责人或期限；'
    "不确定的内容省略。摘要不超过 2000 字。"
)


class MeetingSummarizer(Protocol):
    async def summarize(
        self,
        *,
        meeting_id: UUID,
        title: str,
        segments: list[TranscriptSegment],
        privacy_level: PrivacyLevel,
    ) -> MeetingSummary: ...


class RuleBasedMeetingSummarizer:
    """离线保守兜底，只识别显式标记的决定和行动项。"""

    async def summarize(
        self,
        *,
        meeting_id: UUID,
        title: str,
        segments: list[TranscriptSegment],
        privacy_level: PrivacyLevel,
    ) -> MeetingSummary:
        del meeting_id, privacy_level
        lines = [f"{item.speaker}：{item.text.strip()}" for item in segments if item.text.strip()]
        decisions: list[MeetingDecision] = []
        actions: list[MeetingActionItem] = []
        for item in segments:
            text = item.text.strip()
            decision = re.match(r"^(?:决定|结论)[:：]\s*(.+)$", text)
            if decision:
                decisions.append(MeetingDecision(text=decision.group(1), evidence=text))
            action = re.match(r"^(?:行动项|待办)[:：]\s*(.+)$", text)
            if action:
                actions.append(
                    MeetingActionItem(title=action.group(1), owner=item.speaker, evidence=text)
                )
        body = "\n".join(lines)
        summary = f"{title}共记录 {len(lines)} 条发言。"
        if body:
            summary += "\n" + body[:3_000]
        return MeetingSummary(
            summary=summary,
            decisions=decisions[:50],
            action_items=actions[:100],
        )


class StructuredMeetingSummarizer:
    def __init__(
        self,
        completion: MeetingSummaryCompletionPort,
        *,
        fallback: MeetingSummarizer | None = None,
    ) -> None:
        self._completion = completion
        self._fallback = fallback or RuleBasedMeetingSummarizer()

    async def summarize(
        self,
        *,
        meeting_id: UUID,
        title: str,
        segments: list[TranscriptSegment],
        privacy_level: PrivacyLevel,
    ) -> MeetingSummary:
        privacy_level = PrivacyLevel(privacy_level)
        segments = deepcopy(segments)
        if privacy_level is PrivacyLevel.L3:
            return await self._fallback.summarize(
                meeting_id=meeting_id, title=title, segments=segments, privacy_level=privacy_level
            )
        transcript = "\n".join(f"{item.speaker}：{item.text}" for item in segments)
        request = MeetingSummaryInput(
            meeting_id=meeting_id,
            instruction=MEETING_SUMMARY_INSTRUCTION,
            text=(f"会议：{title}\n转写：\n{transcript}")[:MAX_SUMMARY_INPUT_CHARS],
            privacy_level=privacy_level,
        )
        try:
            result = await self._completion.complete(request)
            payload = MeetingSummary.model_validate(load_json_object(result.text))
            return payload.model_copy(
                update={
                    "action_items": [
                        item.model_copy(update={"status": "proposed", "task_id": None})
                        for item in payload.action_items
                    ]
                }
            )
        except Exception as error:
            if isinstance(error, BudgetDenied) and error.reason_code in {
                "budget_run_inactive",
                "run_source_not_found",
                "model_run_owner_missing",
                "budget_owner_invalid",
            }:
                raise
            logger.warning("meeting summarization fell back meeting=%s", meeting_id, exc_info=True)
            return await self._fallback.summarize(
                meeting_id=meeting_id, title=title, segments=segments, privacy_level=privacy_level
            )
