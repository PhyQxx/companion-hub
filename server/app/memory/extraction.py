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
from .extraction_rules import _load_json_object as _load_json_object
from .extraction_rules import extract_assistant_fact_assertions as extract_assistant_fact_assertions
from .extraction_rules import extraction_instruction as extraction_instruction
from .models import MemoryCandidate, MemoryEntry
from .turn_core import CompletedTurnMemoryExtractor


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
    """Preserve the chat backend API while delegating completed-turn policy."""

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
        return await CompletedTurnMemoryExtractor(
            _BoundMessageExtractor(self._message_extractor, backend)
        ).extract_turn(
            user_text=user_text,
            user_message_id=user_message_id,
            user_occurred_at=user_occurred_at,
            assistant_text=assistant_text,
            assistant_message_id=assistant_message_id,
            assistant_occurred_at=assistant_occurred_at,
            privacy_level=privacy_level,
            retrieved_memories=retrieved_memories,
        )


class _BoundMessageExtractor:
    def __init__(self, extractor: MemoryExtractor, backend: ExtractionBackend | None) -> None:
        self._extractor, self._backend = extractor, backend

    async def extract(
        self,
        text: str,
        *,
        message_id: UUID,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
    ) -> list[MemoryCandidate]:
        return await self._extractor.extract(
            text,
            message_id=message_id,
            privacy_level=privacy_level,
            occurred_at=occurred_at,
            backend=self._backend,
        )


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
