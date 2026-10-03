"""Immutable memory message completion input/result without SDK dependencies."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.schemas.common import PrivacyLevel


@dataclass(frozen=True, slots=True)
class MemoryExtractionInput:
    message_id: UUID
    instruction: str
    text: str
    privacy_level: PrivacyLevel


@dataclass(frozen=True, slots=True)
class MemoryExtractionCompletion:
    text: str


class MemoryExtractionCompletionPort(Protocol):
    async def complete(self, request: MemoryExtractionInput) -> MemoryExtractionCompletion: ...
