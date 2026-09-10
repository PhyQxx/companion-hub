from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.llm import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.llm.factory import build_router
from app.llm.provider import EnvSecretProvider
from app.memory.extraction import _load_json_object
from app.schemas import PrivacyLevel

from .models import MeetingActionItem, MeetingDecision, MeetingSummary, TranscriptSegment

logger = logging.getLogger(__name__)
MAX_SUMMARY_INPUT_CHARS = 100_000


class CompletionBackend(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...


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


class LlmMeetingSummarizer:
    def __init__(
        self,
        config_store: ConfigStore | DatabaseConfigStore,
        *,
        router_builder: Callable[[HubConfig], CompletionBackend] | None = None,
        fallback: MeetingSummarizer | None = None,
    ) -> None:
        self._config_store = config_store
        secrets = EnvSecretProvider()
        self._router_builder = router_builder or (lambda config: build_router(config, secrets))
        self._fallback = fallback or RuleBasedMeetingSummarizer()

    async def summarize(
        self,
        *,
        meeting_id: UUID,
        title: str,
        segments: list[TranscriptSegment],
        privacy_level: PrivacyLevel,
    ) -> MeetingSummary:
        transcript = "\n".join(f"{item.speaker}：{item.text}" for item in segments)
        try:
            snapshot = (
                await self._config_store.refresh()
                if isinstance(self._config_store, DatabaseConfigStore)
                else self._config_store.current
            )
            backend = self._router_builder(snapshot.config)
            result = await backend.complete(
                CompletionRequest(
                    trace_id=meeting_id,
                    messages=[
                        LLMMessage(
                            role="system",
                            content=(
                                "你是会议纪要整理器。只依据转写内容输出严格 JSON："
                                '{"summary":"摘要","decisions":[{"text":"决定",'
                                '"evidence":"原文证据"}],"action_items":[{"title":"任务",'
                                '"owner":"负责人或null","due_at":"ISO时间或null",'
                                '"evidence":"原文证据"}]}。不得补造决定、负责人或期限；'
                                "不确定的内容省略。摘要不超过 2000 字。"
                            ),
                        ),
                        LLMMessage(
                            role="user",
                            content=(
                                f"会议：{title}\n转写：\n{transcript}"
                            )[:MAX_SUMMARY_INPUT_CHARS],
                        ),
                    ],
                    privacy_level=privacy_level,
                    route=(
                        LLMRoute.PRIVATE
                        if privacy_level is PrivacyLevel.L2
                        else LLMRoute.UTILITY
                    ),
                    temperature=0.0,
                    json_mode=True,
                    max_tokens=4_096,
                )
            )
            payload = MeetingSummary.model_validate(_load_json_object(result.text))
            # 模型不能把行动项直接标成已创建，也不能伪造 task_id。
            return payload.model_copy(
                update={
                    "action_items": [
                        item.model_copy(update={"status": "proposed", "task_id": None})
                        for item in payload.action_items
                    ]
                }
            )
        except Exception:
            logger.warning("meeting summarization fell back meeting=%s", meeting_id, exc_info=True)
            return await self._fallback.summarize(
                meeting_id=meeting_id,
                title=title,
                segments=segments,
                privacy_level=privacy_level,
            )
