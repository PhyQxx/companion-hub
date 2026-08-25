from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import Field

from app.cognition import CognitiveDecision
from app.schemas.common import StrictModel


class PerceptionDisposition(StrEnum):
    PROCESSED = "processed"
    SUPPRESSED = "suppressed"
    MERGED = "merged"
    EXPIRED = "expired"
    UNSTABLE = "unstable"


class PerceptionResult(StrictModel):
    event_id: UUID
    disposition: PerceptionDisposition
    reason_code: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    decision: CognitiveDecision | None = None
    merged_into_event_id: UUID | None = None


class ProactivePolicySettings(StrictModel):
    quiet_hours_start: Annotated[
        str,
        Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"),
    ] = "23:00"
    quiet_hours_end: Annotated[
        str,
        Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"),
    ] = "07:00"
    daily_limit: Annotated[int, Field(ge=1, le=50)] = 5
    critical_bypasses_quiet_hours: bool = True
    dedupe_window_seconds: Annotated[int, Field(ge=1, le=86_400)] = 300


class SemanticEventAuditView(StrictModel):
    event_id: UUID
    kind: str
    source_kind: str
    disposition: PerceptionDisposition
    reason_code: str | None = None
    decision_id: UUID | None = None
    merged_into_event_id: UUID | None = None
    evidence_ids: list[str]
    occurred_at: datetime
    expires_at: datetime | None = None
    created_at: datetime
