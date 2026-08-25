from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from app.schemas.common import PrivacyLevel, StrictModel


class DecisionKind(StrEnum):
    IGNORE = "ignore"
    RECORD = "record"
    INFORM = "inform"
    ASK = "ask"
    SUGGEST = "suggest"
    ACT = "act"
    ESCALATE = "escalate"


class Urgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class GoalKind(StrEnum):
    USER = "user"
    SHARED = "shared"
    SYSTEM = "system"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class FeedbackKind(StrEnum):
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    SNOOZED = "snoozed"
    FORBIDDEN = "forbidden"


class SemanticEvent(StrictModel):
    event_id: UUID
    user_id: UUID
    conversation_id: UUID | None = None
    kind: Annotated[str, Field(min_length=1, max_length=160)]
    source_kind: Annotated[str, Field(min_length=1, max_length=80)] = "internal"
    dedupe_key: Annotated[str, Field(min_length=1, max_length=240)] | None = None
    summary: Annotated[str, Field(min_length=1, max_length=500)]
    occurred_at: datetime
    privacy_level: PrivacyLevel
    confidence: Annotated[float, Field(ge=0, le=1)] = 1
    evidence_ids: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    expires_at: datetime | None = None
    passive: bool = False

    @model_validator(mode="after")
    def evidence_required_for_proactive(self) -> SemanticEvent:
        if not self.passive and not self.evidence_ids:
            raise ValueError("proactive semantic events require evidence")
        return self


class GoalView(StrictModel):
    id: UUID
    kind: GoalKind
    title: str
    status: GoalStatus
    source_kind: str
    source_id: str
    due_at: datetime | None = None
    expires_at: datetime | None = None


class WorldState(StrictModel):
    built_at: datetime
    timezone: str
    last_interaction_at: datetime | None = None
    active_capabilities: list[str] = Field(default_factory=list)
    active_goals: list[GoalView] = Field(default_factory=list)
    memory_evidence_ids: list[str] = Field(default_factory=list)
    timeline_evidence_ids: list[str] = Field(default_factory=list)
    recent_proactive_count: int = 0
    same_trigger_recent_count: int = 0
    ignored_same_trigger_count: int = 0
    dnd: bool = False


class AttentionResult(StrictModel):
    score: Annotated[float, Field(ge=0, le=1)]
    threshold: Annotated[float, Field(ge=0, le=1)]
    reason_codes: list[str]
    should_deliberate: bool


class CognitiveDecision(StrictModel):
    id: UUID
    event_id: UUID
    user_id: UUID
    conversation_id: UUID | None = None
    trigger_kind: str
    decision: DecisionKind
    reason_codes: list[str]
    evidence_ids: list[str]
    confidence: Annotated[float, Field(ge=0, le=1)]
    urgency: Urgency
    attention_score: Annotated[float, Field(ge=0, le=1)]
    policy_version: str
    message: Annotated[str, Field(max_length=1_000)] | None = None
    approval_required: bool = False
    model_provider: str | None = None
    model_name: str | None = None
    expires_at: datetime | None = None
    created_at: datetime

    @model_validator(mode="after")
    def safe_first_version(self) -> CognitiveDecision:
        if self.decision == DecisionKind.ACT:
            raise ValueError("autonomous act is disabled in cognitive policy v1")
        if self.decision in {
            DecisionKind.INFORM,
            DecisionKind.ASK,
            DecisionKind.SUGGEST,
            DecisionKind.ESCALATE,
        } and not self.message:
            raise ValueError("visible decisions require a message")
        return self


class CognitiveDecisionView(StrictModel):
    id: UUID
    event_id: UUID
    conversation_id: UUID | None = None
    trigger_kind: str
    decision: DecisionKind
    reason_codes: list[str]
    evidence_ids: list[str]
    confidence: Annotated[float, Field(ge=0, le=1)]
    urgency: Urgency
    attention_score: Annotated[float, Field(ge=0, le=1)]
    policy_version: str
    model_provider: str | None = None
    model_name: str | None = None
    expires_at: datetime | None = None
    created_at: datetime


class ActionResult(StrictModel):
    decision_id: UUID
    outcome: str
    reason_code: str | None = None
    verified: bool = False
    observed_state: dict[str, JsonValue] = Field(default_factory=dict)


class ReflectionCandidate(StrictModel):
    content: str
    evidence_ids: list[str]
    confidence: Annotated[float, Field(ge=0, le=1)]
    requires_confirmation: bool = False
