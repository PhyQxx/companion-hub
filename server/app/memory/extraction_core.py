"""Memory candidate policy with detached completion and offline rule fallback."""

from datetime import datetime
from math import isfinite
from uuid import UUID

from app.harness.json_payload import load_json_object
from app.schemas.common import PrivacyLevel

from .extraction_ports import MemoryExtractionCompletionPort, MemoryExtractionInput
from .extraction_rules import (
    LLM_EXTRACTOR_VERSION,
    MAX_EXTRACT_INPUT_CHARS,
    MAX_LLM_CANDIDATES,
    RuleBasedExtractor,
    _infer_user_fact_key,
    extraction_instruction,
)
from .models import ExtractedCandidates, MemoryCandidate, MemorySourceKind, MemorySourceRef


class StructuredMemoryExtractor:
    def __init__(self, *, fallback: RuleBasedExtractor | None = None) -> None:
        self._fallback = fallback or RuleBasedExtractor()

    async def extract(
        self,
        text: str,
        *,
        message_id: UUID,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
        completion: MemoryExtractionCompletionPort | None = None,
    ) -> list[MemoryCandidate]:
        privacy_level = PrivacyLevel(privacy_level)
        if privacy_level is PrivacyLevel.L3:
            return []
        if completion is None:
            return await self._fallback.extract(
                text, message_id=message_id, privacy_level=privacy_level, occurred_at=occurred_at
            )
        request = MemoryExtractionInput(
            message_id=message_id,
            instruction=extraction_instruction(privacy_level),
            text=text[:MAX_EXTRACT_INPUT_CHARS],
            privacy_level=privacy_level,
        )
        try:
            result = await completion.complete(request)
            payload = ExtractedCandidates.model_validate(load_json_object(result.text))
            if any(
                not isfinite(item.importance)
                or (
                    item.confidence is not None
                    and (not isfinite(item.confidence) or not 0 <= item.confidence <= 1)
                )
                for item in payload.candidates
            ):
                raise ValueError("memory_candidate_score_invalid")
        except Exception:
            return await self._fallback.extract(
                text, message_id=message_id, privacy_level=privacy_level, occurred_at=occurred_at
            )
        return [
            MemoryCandidate(
                type=item.type,
                content=item.content,
                fact_key=_infer_user_fact_key(item.content),
                privacy_level=privacy_level,
                sources=[
                    MemorySourceRef(
                        source_kind=MemorySourceKind.MESSAGE,
                        source_id=str(message_id),
                    )
                ],
                importance=min(1.0, max(0.0, item.importance)),
                confidence=item.confidence,
                extractor_version=LLM_EXTRACTOR_VERSION,
                valid_from=occurred_at,
            )
            for item in payload.candidates[:MAX_LLM_CANDIDATES]
        ]
