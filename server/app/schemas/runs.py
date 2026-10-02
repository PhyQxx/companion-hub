from datetime import datetime
from uuid import UUID

from pydantic import Field

from .common import StrictModel
from .execution import RunActionOutcome


class RunBudgetView(StrictModel):
    enabled: bool
    max_llm_attempts: int
    max_tokens: int
    llm_attempts: int
    charged_tokens: int
    unsettled_calls: int | None = None
    unknown_usage_calls: int | None = None


class RunCriterionView(StrictModel):
    kind: str
    source_id: UUID
    status: str
    validation_level: str = "V0"
    reason_code: str | None = None


class RunGoalView(StrictModel):
    scope: str = "declared_run_contract"
    status: str = "not_declared"
    required: int = 0
    passed: int = 0
    pending: int = 0
    failed: int = 0
    inconclusive: int = 0
    criteria: list[RunCriterionView] = Field(default_factory=list)


class RunView(StrictModel):
    goal: RunGoalView | None = None
    budget_summary: RunBudgetView | None = None
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
