"""Pure commitment recognition, validation and source-based acceptance policy."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field, TypeAdapter

from app.harness.json_payload import load_json_object
from app.schemas.common import PrivacyLevel, StrictModel

from .commitment_ports import CommitmentCompletionPort, CommitmentInput, CommitmentRepository
from .models import GoalKind, GoalView

logger = logging.getLogger("app.cognition.goals")

MAX_EXTRACT_INPUT_CHARS = 4_000
MIN_COMMITMENT_CONFIDENCE = 0.7
MAX_COMMITMENTS_PER_MESSAGE = 3


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
    if privacy_level == PrivacyLevel.L2:
        instruction += "\n隐私约束：title 只能是事件级概括，严禁任何生理、身体或露骨细节。"
    return instruction


class CommitmentTracker:
    """聊天后台调用：提取承诺 → 以消息为证据创建目标（幂等）。"""

    def __init__(self, store: CommitmentRepository) -> None:
        self._store = store

    async def ingest_message(
        self,
        *,
        user_id: UUID,
        message_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
        completion: CommitmentCompletionPort | None = None,
        strict: bool = False,
    ) -> list[GoalView]:
        privacy_level = PrivacyLevel(privacy_level)
        if privacy_level is PrivacyLevel.L3 or completion is None or not text.strip():
            return []
        request = CommitmentInput(message_id, text[:MAX_EXTRACT_INPUT_CHARS], privacy_level)
        try:
            result = await completion.complete(request)
        except Exception:
            if strict:
                raise
            return []
        try:
            payload = _commitment_list_adapter.validate_python(load_json_object(result.text))
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
                privacy_level=privacy_level,
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
        privacy_level: PrivacyLevel,
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
                privacy_level=privacy_level,
            )
        except ValueError:
            logger.debug("commitment goal rejected for %s", message_id, exc_info=True)
            return None
