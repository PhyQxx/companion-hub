"""Content-free projections of execution and verification evidence."""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field

from .common import StrictModel


class ValidationLevel(StrEnum):
    V0 = "V0"
    V1 = "V1"
    V2 = "V2"
    V3 = "V3"
    V4 = "V4"


class EvidenceRef(StrictModel):
    kind: str
    source_id: str


class ExecutionOutcome(StrictModel):
    execution_status: Literal[
        "pending", "running", "succeeded", "failed", "cancelled", "unknown_outcome"
    ]
    side_effect_state: Literal["none", "not_started", "submitted", "confirmed", "unknown"]
    validation_status: Literal["unverified", "passed", "inconclusive"]
    validation_level: ValidationLevel = ValidationLevel.V0
    retry_class: Literal["safe_read", "manual_reconcile", "never"]
    reason_code: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class RunActionOutcome(StrictModel):
    plan_id: UUID
    step_id: UUID
    outcome: ExecutionOutcome
