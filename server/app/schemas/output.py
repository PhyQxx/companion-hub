from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from .common import (
    UUID7,
    OutputContentPart,
    Priority,
    PrivacyLevel,
    StrictModel,
    TokenName,
)


class OutputKind(StrEnum):
    REPLY = "reply"
    PROACTIVE = "proactive"
    SYSTEM = "system"
    ALERT = "alert"


class Presentation(StrictModel):
    emotion: str | None = None
    intensity: Annotated[float, Field(ge=0, le=1)] | None = None
    gesture: str | None = None


class Audience(StrictModel):
    mode: Literal["user", "admins", "public"] = "user"


class TargetSelector(StrictModel):
    mode: Literal["best_available", "all_compatible", "explicit"] = "best_available"
    prefer: list[str] = Field(default_factory=list, max_length=16)
    endpoint_ids: list[str] = Field(default_factory=list, max_length=32)


class DeliveryPolicy(StrictModel):
    priority: Priority = Priority.NORMAL
    ttl_ms: Annotated[int, Field(gt=0, le=86_400_000)] = 8_000
    interruptible: bool = True
    ack_policy: Literal["none", "accepted", "delivered", "played"] = "accepted"
    fallback: list[TokenName] = Field(default_factory=list, max_length=16)


class OutputIntent(StrictModel):
    proto_version: Literal[1] = 1
    schema_ref: Literal["aria.output-intent/1"] = "aria.output-intent/1"
    intent_id: UUID7
    correlation_id: UUID7
    causation_id: UUID7 | None = None
    user_id: UUID7
    conversation_id: UUID7 | None = None
    turn_id: UUID7 | None = None
    generation_id: UUID7 | None = None
    kind: OutputKind
    privacy_level: PrivacyLevel = PrivacyLevel.L1
    content: Annotated[list[OutputContentPart], Field(min_length=1, max_length=32)]
    presentation: Presentation = Field(default_factory=Presentation)
    audience: Audience = Field(default_factory=Audience)
    target_selector: TargetSelector = Field(default_factory=TargetSelector)
    delivery: DeliveryPolicy = Field(default_factory=DeliveryPolicy)
    created_at: AwareDatetime


class DeliveryPlan(StrictModel):
    delivery_id: UUID7
    intent_id: UUID7
    adapter_instance_id: UUID7
    endpoint_id: Annotated[str, Field(min_length=1, max_length=160)]
    generation_id: UUID7 | None = None
    privacy_level: PrivacyLevel
    selected_content: Annotated[list[OutputContentPart], Field(min_length=1, max_length=32)]
    presentation: Presentation = Field(default_factory=Presentation)
    deadline_at: AwareDatetime
    attempt: Annotated[int, Field(ge=1)] = 1
    idempotency_key: Annotated[str, Field(min_length=8, max_length=255)]


class DeliveryStatus(StrEnum):
    PLANNED = "planned"
    SENDING = "sending"
    ACCEPTED = "accepted"
    PLAYING = "playing"
    DELIVERED = "delivered"
    DROPPED = "dropped"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN_OUTCOME = "unknown_outcome"


class DeliveryReceipt(StrictModel):
    delivery_id: UUID7
    intent_id: UUID7
    adapter_instance_id: UUID7
    endpoint_id: Annotated[str, Field(min_length=1, max_length=160)]
    status: DeliveryStatus
    attempt: Annotated[int, Field(ge=1)]
    generation_id: UUID7 | None = None
    occurred_at: AwareDatetime
    reason_code: TokenName | None = None
    external_operation_id: str | None = None
