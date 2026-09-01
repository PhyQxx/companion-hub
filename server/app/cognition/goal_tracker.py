"""GOAL-01 承诺跟踪：从聊天消息中识别明确承诺并落为可提醒的目标。

识别原则（对应验收"不把随口表达当承诺"）：
- 只接受用户第一人称的明确承诺/约定：有具体动作，常含明确时间；
- 疑问、假设、愿望、模糊意向、第三人称转述一律不提取；
- 无可用 utility 后端或模型输出不合法时不提取（与记忆提取不同，
  这里不做规则兜底——宁可漏掉也不误建目标）；
- 每条消息以 source_id(message id) 做天然幂等，同一消息不重复建目标。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Protocol
from uuid import UUID

from pydantic import Field, TypeAdapter

from app.llm import CompletionRequest, LLMMessage, LLMRoute
from app.schemas.common import PrivacyLevel, StrictModel

from .models import GoalKind, GoalView
from .store import CognitiveStore

logger = logging.getLogger("app.cognition.goals")

MAX_EXTRACT_INPUT_CHARS = 4_000
MIN_COMMITMENT_CONFIDENCE = 0.7
MAX_COMMITMENTS_PER_MESSAGE = 3


class ExtractionBackend(Protocol):
    async def complete(self, request: CompletionRequest) -> object: ...


class _Commitment(StrictModel):
    title: Annotated[str, Field(min_length=2, max_length=320)]
    due_at: datetime | None = None
    confidence: Annotated[float, Field(ge=0, le=1)]


class _CommitmentList(StrictModel):
    commitments: Annotated[list[_Commitment], Field(max_length=MAX_COMMITMENTS_PER_MESSAGE)] = (
        Field(default_factory=list)
    )


_commitment_list_adapter = TypeAdapter(_CommitmentList)


def commitment_instruction(privacy_level: PrivacyLevel) -> str:
    instruction = (
        "你是承诺识别器。判断用户消息里是否有用户本人做出的明确承诺或约定，只输出严格 JSON：\n"
        '{"commitments":[{"title":"……","due_at":"2026-09-10T09:00:00+08:00","confidence":0.9}]}\n'
        "要求：\n"
        "1. 只提取第一人称明确承诺：我答应/我约了/我必须/我明天要交……有具体动作；\n"
        "2. 疑问句、假设（如果/要是）、愿望（好想）、模糊意向（可能/也许/看情况）、"
        "转述他人一律不提取；\n"
        "3. title 用不超过 40 字的中文短语概括承诺内容；\n"
        "4. due_at 仅在用户说了明确时间时给出（推断时区 Asia/Shanghai），否则为 null；\n"
        '5. confidence < 0.7 的不要输出；没有承诺时输出 {"commitments":[]}。'
    )
    if privacy_level is PrivacyLevel.L2:
        instruction += "\n隐私约束：title 只能是事件级概括，严禁任何生理、身体或露骨细节。"
    return instruction


class GoalTracker:
    """聊天后台调用：提取承诺 → 以消息为证据创建目标（幂等）。"""

    def __init__(self, store: CognitiveStore) -> None:
        self._store = store

    async def ingest_message(
        self,
        *,
        user_id: UUID,
        message_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
        backend: ExtractionBackend | None = None,
    ) -> list[GoalView]:
        if privacy_level is PrivacyLevel.L3 or backend is None or not text.strip():
            return []
        request = CompletionRequest(
            trace_id=message_id,
            messages=[
                LLMMessage(role="system", content=commitment_instruction(privacy_level)),
                LLMMessage(role="user", content=text[:MAX_EXTRACT_INPUT_CHARS]),
            ],
            privacy_level=privacy_level,
            route=LLMRoute.UTILITY,
            temperature=0.0,
            json_mode=True,
        )
        try:
            from app.memory.extraction import _load_json_object

            result = await backend.complete(request)
            payload = _commitment_list_adapter.validate_python(
                _load_json_object(str(getattr(result, "text", "")))
            )
        except Exception:
            logger.debug("commitment extraction skipped for %s", message_id, exc_info=True)
            return []
        created: list[GoalView] = []
        for item in payload.commitments:
            if item.confidence < MIN_COMMITMENT_CONFIDENCE:
                continue
            goal = await self._create_once(
                user_id=user_id,
                message_id=message_id,
                title=item.title,
                due_at=item.due_at,
            )
            if goal is not None:
                created.append(goal)
        return created

    async def _create_once(
        self,
        *,
        user_id: UUID,
        message_id: UUID,
        title: str,
        due_at: datetime | None,
    ) -> GoalView | None:
        source_id = str(message_id)
        existing = await self._store.goal_by_source(
            user_id, source_kind="message", source_id=source_id
        )
        if existing is not None:
            return None
        try:
            return await self._store.create_goal(
                user_id=user_id,
                kind=GoalKind.USER,
                title=title,
                source_kind="message",
                source_id=source_id,
                due_at=due_at,
            )
        except ValueError:
            logger.debug("commitment goal rejected for %s", message_id, exc_info=True)
            return None
