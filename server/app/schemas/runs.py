from datetime import datetime
from uuid import UUID

from pydantic import Field

from .common import StrictModel
from .execution import RunActionOutcome


class RunView(StrictModel):
    action_outcomes: list[RunActionOutcome] = Field(default_factory=list)
    job_ids: list[UUID] = Field(default_factory=list)
    plan_ids: list[UUID] = Field(default_factory=list)
    id: UUID
    conversation_id: UUID | None
    status: str
    state_version: int
    cancel_epoch: int
    privacy_level: str
    config_version: int | None
    persona_version: int | None
    created_at: datetime
    updated_at: datetime


class RunEventView(StrictModel):
    event_id: UUID
    seq: int
    kind: str
    schema_version: int
    payload: dict[str, object]
    privacy_level: str
    occurred_at: datetime
