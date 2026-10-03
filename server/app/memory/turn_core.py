"""Completed-turn memory policy, independent of model requests and persistence."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.schemas.common import PrivacyLevel

from .extraction_rules import (
    RuleBasedExtractor,
    _dedupe_and_suppress_echo,
    _extract_assistant_candidates,
    _extract_user_directed_candidates,
)
from .models import MemoryCandidate, MemoryEntry


class MessageMemoryExtractor(Protocol):
    async def extract(
        self,
        text: str,
        *,
        message_id: UUID,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
    ) -> list[MemoryCandidate]: ...


class CompletedTurnMemoryExtractor:
    def __init__(self, message_extractor: MessageMemoryExtractor | None = None) -> None:
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
    ) -> list[MemoryCandidate]:
        privacy = PrivacyLevel(privacy_level)
        if privacy is PrivacyLevel.L3:
            return []
        # Entries are frozen scalar views. Own the collection before awaiting
        # the message port so caller list edits cannot alter echo evidence.
        memories = tuple(retrieved_memories)
        extracted = await self._message_extractor.extract(
            user_text,
            message_id=user_message_id,
            privacy_level=privacy,
            occurred_at=user_occurred_at,
        )
        # Ports may retain their collection and nested mutable source refs.
        candidates = [candidate.model_copy(deep=True) for candidate in extracted]
        if privacy is PrivacyLevel.L2:
            return candidates
        candidates.extend(
            _extract_user_directed_candidates(
                user_text, message_id=user_message_id, occurred_at=user_occurred_at
            )
        )
        candidates.extend(
            _extract_assistant_candidates(
                assistant_text,
                message_id=assistant_message_id,
                occurred_at=assistant_occurred_at,
            )
        )
        return _dedupe_and_suppress_echo(candidates, memories)
