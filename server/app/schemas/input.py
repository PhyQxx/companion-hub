from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .common import (
    UUID7,
    EphemeralContentPart,
    InputContentPart,
    NamespacedName,
    Priority,
    PrivacyLevel,
    SourceRef,
    StrictModel,
)


class InputEnvelope(StrictModel):
    proto_version: Literal[1] = 1
    schema_ref: Literal["aria.input-envelope/1"] = "aria.input-envelope/1"
    event_id: UUID7
    correlation_id: UUID7
    causation_id: UUID7 | None = None
    user_id: UUID7
    conversation_id: UUID7 | None = None
    turn_id: UUID7 | None = None
    source: SourceRef
    kind: NamespacedName
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    priority: Priority = Priority.NORMAL
    privacy_level: PrivacyLevel
    content: Annotated[list[InputContentPart], Field(min_length=1, max_length=32)]
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("privacy_level")
    @classmethod
    def durable_events_reject_l3(cls, value: PrivacyLevel) -> PrivacyLevel:
        if value == PrivacyLevel.L3:
            raise ValueError("L3 content must use EphemeralSignal")
        return value

    @field_validator("extensions")
    @classmethod
    def extensions_are_namespaced(
        cls, value: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        invalid = [key for key in value if "." not in key]
        if invalid:
            raise ValueError(f"extension keys must be namespaced: {', '.join(invalid)}")
        return value

    @model_validator(mode="after")
    def validate_conversation_identifiers(self) -> InputEnvelope:
        if self.kind in {"message.received", "speech.transcribed"} and (
            self.conversation_id is None or self.turn_id is None
        ):
            raise ValueError("conversation inputs require conversation_id and turn_id")
        if self.received_at < self.occurred_at:
            raise ValueError("received_at cannot be earlier than occurred_at")
        return self


class EphemeralSignal(StrictModel):
    signal_id: UUID7
    source: SourceRef
    channel: Annotated[str, Field(min_length=1, max_length=120)]
    occurred_at: AwareDatetime
    privacy_level: PrivacyLevel
    content: EphemeralContentPart
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def expiry_follows_occurrence(self) -> EphemeralSignal:
        if self.expires_at <= self.occurred_at:
            raise ValueError("expires_at must be later than occurred_at")
        return self
