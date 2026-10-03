"""Compatibility adapter for the existing budgeted chat completion backend."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.llm.contracts import CompletionRequest, LLMMessage, LLMRoute
from app.schemas.common import PrivacyLevel

from .commitment_ports import CommitmentCompletion, CommitmentInput, CommitmentRepository
from .commitments import MAX_COMMITMENTS_PER_MESSAGE as MAX_COMMITMENTS_PER_MESSAGE
from .commitments import MAX_EXTRACT_INPUT_CHARS as MAX_EXTRACT_INPUT_CHARS
from .commitments import MIN_COMMITMENT_CONFIDENCE as MIN_COMMITMENT_CONFIDENCE
from .commitments import CommitmentTracker
from .commitments import commitment_instruction as commitment_instruction
from .models import GoalView


class ExtractionBackend(Protocol):
    async def complete(self, request: CompletionRequest) -> object: ...


class _ChatCommitmentCompletion:
    def __init__(self, backend: ExtractionBackend) -> None:
        self._backend = backend

    async def complete(self, request: CommitmentInput) -> CommitmentCompletion:
        result = await self._backend.complete(
            CompletionRequest(
                trace_id=request.message_id,
                messages=[
                    LLMMessage(
                        role="system", content=commitment_instruction(request.privacy_level)
                    ),
                    LLMMessage(role="user", content=request.text),
                ],
                privacy_level=request.privacy_level,
                route=LLMRoute.UTILITY,
                temperature=0.0,
                json_mode=True,
            )
        )
        return CommitmentCompletion(str(getattr(result, "text", "")))


class GoalTracker:
    """Keep the existing chat API while injecting detached ports into policy."""

    def __init__(self, store: CommitmentRepository) -> None:
        self._tracker = CommitmentTracker(store)

    async def ingest_message(
        self,
        *,
        user_id: UUID,
        message_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
        backend: ExtractionBackend | None = None,
        strict: bool = False,
    ) -> list[GoalView]:
        return await self._tracker.ingest_message(
            user_id=user_id,
            message_id=message_id,
            text=text,
            privacy_level=privacy_level,
            completion=_ChatCommitmentCompletion(backend) if backend is not None else None,
            strict=strict,
        )
