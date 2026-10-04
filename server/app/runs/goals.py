"""Detach domain ORM rows before evaluating the declared run contract."""

from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from app.db import ActionPlanRecord, ActionStepRecord, JobRecord, TaskRunRecord
from app.harness.criteria import (
    JobCriteriaSnapshot,
    PlanCriteriaSnapshot,
    RunCriteriaSnapshot,
    StepCriteriaSnapshot,
    WorkRequirement,
    evaluate_criteria,
)
from app.schemas.common import TokenName
from app.schemas.runs import RunGoalView

from .outcomes import action_outcome

_CHANNELS = TypeAdapter(list[TokenName])
MAX_REQUIREMENTS = 1000


def _requirements(value: object) -> tuple[WorkRequirement, ...] | None:
    if value is None:
        return None  # Retain the legacy associations scope for old runs.
    invalid = (WorkRequirement("invalid", None),)
    if not isinstance(value, list) or len(value) > MAX_REQUIREMENTS:
        return invalid
    result: list[WorkRequirement] = []
    seen: set[tuple[str, UUID]] = set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
            return invalid
        identifier = item.get("id")
        if not isinstance(identifier, str):
            return invalid
        try:
            work_id = UUID(identifier)
        except ValueError:
            return invalid
        key = (item["kind"], work_id)
        if key in seen:
            return invalid
        seen.add(key)
        result.append(WorkRequirement(item["kind"], work_id))
    return tuple(result)


def goal_view(
    run: TaskRunRecord,
    plans: list[ActionPlanRecord],
    steps: list[ActionStepRecord],
    jobs: list[JobRecord],
) -> RunGoalView:
    contract = run.contract
    if not isinstance(contract, dict):
        return evaluate_criteria(
            RunCriteriaSnapshot(run.id, run.status, "invalid_contract", ()), (), (), ()
        )
    channels = contract.get("delivery_channels")
    reported = False
    if isinstance(channels, list) and 1 <= len(channels) <= 16:
        try:
            _CHANNELS.validate_python(channels)
            reported = True
        except ValidationError:
            pass
    root = contract.get("criterion")
    reason = (
        contract.get(
            "delivery_result"
            if root in {"audio_frames_sent", "voice_reply_sent", "text_with_optional_audio_sent"}
            else "delivery_reason"
        )
        if isinstance(root, str)
        else contract.get("delivery_reason")
    )
    dispatch = contract.get("dispatch_state")
    return evaluate_criteria(
        RunCriteriaSnapshot(
            id=run.id,
            status=run.status,
            criterion=root if isinstance(root, str) or root is None else "invalid_contract",
            required_work=_requirements(contract.get("required_work")),
            dispatch_state=dispatch if isinstance(dispatch, str) else None,
            delivery_reason=reason if isinstance(reason, str) else None,
            reported_channels=reported,
        ),
        tuple(PlanCriteriaSnapshot(plan.id, plan.status, plan.reason_code) for plan in plans),
        tuple(
            StepCriteriaSnapshot(step.plan_id, step.status, action_outcome(step)) for step in steps
        ),
        tuple(JobCriteriaSnapshot(job.id, job.status, job.kind, job.error_code) for job in jobs),
    )
