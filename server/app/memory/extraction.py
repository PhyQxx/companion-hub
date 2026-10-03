"""Legacy turn/message APIs composed with detached memory extraction policy."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, final
from uuid import UUID

from app.llm import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.schemas.common import PrivacyLevel

from .extraction_core import StructuredMemoryExtractor
from .extraction_ports import MemoryExtractionCompletion, MemoryExtractionInput
from .extraction_rules import LLM_EXTRACTOR_VERSION as LLM_EXTRACTOR_VERSION
from .extraction_rules import MAX_EXTRACT_INPUT_CHARS as MAX_EXTRACT_INPUT_CHARS
from .extraction_rules import MAX_LLM_CANDIDATES as MAX_LLM_CANDIDATES
from .extraction_rules import RULE_EXTRACTOR_VERSION as RULE_EXTRACTOR_VERSION
from .extraction_rules import RuleBasedExtractor as RuleBasedExtractor
from .extraction_rules import (
    _dedupe_and_suppress_echo,
    _extract_assistant_candidates,
    _extract_user_directed_candidates,
)
from .extraction_rules import _load_json_object as _load_json_object
from .extraction_rules import extract_assistant_fact_assertions as extract_assistant_fact_assertions
from .extraction_rules import extraction_instruction as extraction_instruction
from .models import MemoryCandidate, MemoryEntry


class ExtractionBackend(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...


class MemoryExtractor(Protocol):
    async def extract(
        self,
        text: str,
        *,
        message_id: UUID,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
        backend: ExtractionBackend | None = None,
    ) -> list[MemoryCandidate]: ...


@final
class TurnMemoryExtractor:
    """在已完成回合上提取 user / assistant / shared 三类候选。

    现有 message extractor 继续负责用户事实和 L2 脱敏；助手自述与 shared
    约定先使用保守的确定性规则，避免为 Batch C 额外增加一次 utility 调用。
    后续可在不改变调用接口的前提下升级为一次性 full-turn LLM extractor。
    """

    def __init__(self, message_extractor: MemoryExtractor | None = None) -> None:
        self._message_extractor = message_extractor or RuleBasedExtractor()

    async def extract_turn(
        self,
        *,
        user_text: str,
        user_message_id: UUID,
        user_occurred_at: datetime,
        assistant_text: str,
        assistant_message_id: UUID,
        assistant_occurred_at: datetime,
        privacy_level: PrivacyLevel,
        retrieved_memories: Sequence[MemoryEntry] = (),
        backend: ExtractionBackend | None = None,
    ) -> list[MemoryCandidate]:
        privacy = PrivacyLevel(privacy_level)
        if privacy is PrivacyLevel.L3:
            return []

        candidates = await self._message_extractor.extract(
            user_text,
            message_id=user_message_id,
            privacy_level=privacy,
            occurred_at=user_occurred_at,
            backend=backend,
        )

        # L2 只允许已有 LLM 脱敏提取器产生事件级候选；确定性规则没有
        # 足够的脱敏能力，因此不从用户/助手正文再提取主体事实。
        if privacy is PrivacyLevel.L2:
            return candidates

        candidates.extend(
            _extract_user_directed_candidates(
                user_text,
                message_id=user_message_id,
                occurred_at=user_occurred_at,
            )
        )
        candidates.extend(
            _extract_assistant_candidates(
                assistant_text,
                message_id=assistant_message_id,
                occurred_at=assistant_occurred_at,
            )
        )
        return _dedupe_and_suppress_echo(candidates, retrieved_memories)


class _ChatMemoryCompletion:
    def __init__(self, backend: ExtractionBackend) -> None:
        self._backend = backend

    async def complete(self, request: MemoryExtractionInput) -> MemoryExtractionCompletion:
        result = await self._backend.complete(
            CompletionRequest(
                trace_id=request.message_id,
                messages=[
                    LLMMessage(role="system", content=request.instruction),
                    LLMMessage(role="user", content=request.text),
                ],
                privacy_level=request.privacy_level,
                route=LLMRoute.UTILITY,
                temperature=0.1,
                json_mode=True,
            )
        )
        return MemoryExtractionCompletion(result.text)


@final
class LlmMemoryExtractor:
    def __init__(self, *, fallback: RuleBasedExtractor | None = None) -> None:
        self._core = StructuredMemoryExtractor(fallback=fallback)

    async def extract(
        self,
        text: str,
        *,
        message_id: UUID,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
        backend: ExtractionBackend | None = None,
    ) -> list[MemoryCandidate]:
        return await self._core.extract(
            text,
            message_id=message_id,
            privacy_level=privacy_level,
            occurred_at=occurred_at,
            completion=_ChatMemoryCompletion(backend) if backend is not None else None,
        )
