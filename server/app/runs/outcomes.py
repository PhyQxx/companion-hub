"""Conservative adapters: domain completion is not proof of business success."""

from app.db import ActionStepRecord
from app.schemas.execution import EvidenceRef, ExecutionOutcome, ValidationLevel


def action_outcome(step: ActionStepRecord) -> ExecutionOutcome:
    status = {
        "completed": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
        "skipped": "cancelled",
        "expired": "cancelled",
        "executing": "running",
        "unknown_outcome": "unknown_outcome",
    }.get(step.status, "pending")
    verified = step.verification_status == "verified" and status == "succeeded"
    level = ValidationLevel.V0
    if verified and step.verification_policy == "receipt":
        level = ValidationLevel.V1
    if (
        verified
        and step.verification_policy == "read_after_write"
        and step.verifier_id == "home.entity_state"
    ):
        level = ValidationLevel.V3
    started = step.started_at is not None or status in {"running", "succeeded", "unknown_outcome"}
    side_effect = "none" if step.risk == "A0" else "not_started"
    admission_rejected = (step.result or {}).get("admission_status") == "not_admitted"
    if step.risk != "A0" and started and not admission_rejected:
        side_effect = "confirmed" if level == ValidationLevel.V3 else "submitted"
        if status in {"failed", "unknown_outcome", "cancelled", "running"}:
            side_effect = "unknown"
    return ExecutionOutcome.model_validate(
        {
            "execution_status": status,
            "side_effect_state": side_effect,
            "validation_status": "passed"
            if level != ValidationLevel.V0
            else "inconclusive"
            if step.verification_status == "inconclusive"
            else "unverified",
            "validation_level": level,
            "retry_class": "manual_reconcile"
            if side_effect == "unknown"
            else "safe_read"
            if step.risk == "A0"
            else "never",
            "reason_code": step.reason_code,
            "evidence_refs": [EvidenceRef(kind="action_step", source_id=str(step.id))]
            if level != ValidationLevel.V0
            else [],
        }
    )
