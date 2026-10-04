"""Pure conservative aggregation over content-free, detached execution snapshots."""

from dataclasses import dataclass
from uuid import UUID

from app.schemas.execution import ExecutionOutcome, ValidationLevel
from app.schemas.runs import RunCriterionView, RunGoalView


@dataclass(frozen=True, slots=True)
class WorkRequirement:
    kind: str
    source_id: UUID | None


@dataclass(frozen=True, slots=True)
class RunCriteriaSnapshot:
    id: UUID
    status: str
    criterion: str | None
    required_work: tuple[WorkRequirement, ...] | None
    dispatch_state: str | None = None
    delivery_reason: str | None = None
    reported_channels: bool = False


@dataclass(frozen=True, slots=True)
class PlanCriteriaSnapshot:
    id: UUID
    status: str
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class StepCriteriaSnapshot:
    plan_id: UUID
    status: str
    outcome: ExecutionOutcome


@dataclass(frozen=True, slots=True)
class JobCriteriaSnapshot:
    id: UUID
    status: str
    kind: str
    error_code: str | None


def evaluate_criteria(
    run: RunCriteriaSnapshot,
    plans: tuple[PlanCriteriaSnapshot, ...],
    steps: tuple[StepCriteriaSnapshot, ...],
    jobs: tuple[JobCriteriaSnapshot, ...],
) -> RunGoalView:
    criteria: list[RunCriterionView] = []
    root = run.criterion
    if root == "reply_committed":
        state = (
            "passed"
            if run.status == "succeeded"
            else "failed"
            if run.status in {"failed", "cancelled"}
            else "pending"
        )
        criteria.append(
            RunCriterionView(
                kind=root,
                source_id=run.id,
                status=state,
                validation_level="V1" if state == "passed" else "V0",
            )
        )
    elif root in {"audio_frames_sent", "voice_reply_sent"}:
        reason = run.delivery_reason
        if run.status in {"accepted", "running"}:
            state, reason = "pending", None
        elif run.status == "succeeded" and reason == root:
            state, reason = "passed", None
        elif reason == "expired_delivery_unknown":
            state = "inconclusive"
        elif reason == root:
            state, reason = "inconclusive", "voice_delivery_returned_after_stop"
        elif run.status == "succeeded":
            state, reason = "inconclusive", "voice_delivery_evidence_missing"
        else:
            state = "failed"
        criteria.append(
            RunCriterionView(
                kind=root,
                source_id=run.id,
                status=state,
                validation_level="V1" if state == "passed" else "V0",
                reason_code=reason,
            )
        )
    elif root == "delivery_channels_returned":
        dispatch = run.dispatch_state
        reason = run.delivery_reason
        reported = run.reported_channels
        if run.status in {"accepted", "running"}:
            state = "pending"
        elif dispatch == "unknown" or (
            dispatch == "started" and run.status in {"failed", "cancelled"}
        ):
            state, reason = "inconclusive", "delivery_outcome_unknown"
        elif reported and run.status != "succeeded":
            state, reason = "inconclusive", "delivery_returned_after_stop"
        elif run.status == "succeeded" and dispatch == "returned" and reported:
            state = "passed"
        else:
            state = "failed"
        criteria.append(
            RunCriterionView(
                kind=root,
                source_id=run.id,
                status=state,
                validation_level="V1" if reported else "V0",
                reason_code=str(reason) if reason else None,
            )
        )
    elif root == "model_result_returned":
        state = (
            "inconclusive"
            if run.status == "succeeded"
            else "failed"
            if run.status in {"failed", "cancelled"}
            else "pending"
        )
        criteria.append(
            RunCriterionView(
                kind=root,
                source_id=run.id,
                status=state,
                reason_code="model_result_unverified" if state == "inconclusive" else None,
            )
        )
    elif root not in {None, "handler_completed"}:
        criteria.append(
            RunCriterionView(
                kind="unsupported_criterion",
                source_id=run.id,
                status="inconclusive",
                reason_code="criterion_invalid"
                if root == "invalid_contract"
                else "criterion_not_supported",
            )
        )
    registered = run.required_work
    # Older runs can report existing links, but do not pretend these were
    # explicitly enrolled when the original contract was accepted.
    legacy = registered is None
    work = (
        registered
        if registered is not None
        else [
            *[WorkRequirement("action_plan", plan.id) for plan in plans],
            *[
                WorkRequirement("delegated_job", job.id)
                for job in jobs
                if job.kind.startswith("deleg.")
            ],
        ]
    )
    if root == "handler_completed" and not any(item.source_id == run.id for item in work):
        work = [*work, WorkRequirement("delegated_job", run.id)]
    for item in work:
        source_id = item.source_id
        if source_id is None:
            criteria.append(
                RunCriterionView(
                    kind="unsupported_criterion",
                    source_id=run.id,
                    status="inconclusive",
                    reason_code="criterion_invalid",
                )
            )
            continue
        if item.kind == "action_plan":
            plan = next((plan for plan in plans if plan.id == source_id), None)
            actions = [step for step in steps if step.plan_id == source_id]
            reason = None
            level = "V0"
            if plan is None:
                state, reason = "inconclusive", "required_plan_missing"
            elif any(step.status == "unknown_outcome" for step in actions):
                state, reason = "inconclusive", "action_outcome_unknown"
            elif plan.status in {"failed", "cancelled", "expired", "partially_completed"}:
                state = "failed"
                reason = plan.reason_code or "required_plan_not_completed"
            elif plan.status != "completed":
                state = "pending"
            else:
                outcomes = [step.outcome for step in actions]
                if outcomes and all(
                    outcome.validation_status == "passed"
                    and outcome.validation_level != ValidationLevel.V0
                    and outcome.execution_status == "succeeded"
                    and outcome.side_effect_state != "unknown"
                    and outcome.evidence_refs
                    for outcome in outcomes
                ):
                    state = "passed"
                    level = min(str(outcome.validation_level) for outcome in outcomes)
                else:
                    state, reason = "inconclusive", "required_plan_unverified"
            criteria.append(
                RunCriterionView(
                    kind="action_plan_verified",
                    source_id=source_id,
                    status=state,
                    validation_level=level,
                    reason_code=reason,
                )
            )
        elif item.kind == "delegated_job":
            job = next((job for job in jobs if job.id == source_id), None)
            if job is None:
                state, reason = "inconclusive", "required_job_missing"
            elif job.status in {"failed", "cancelled"}:
                state, reason = "failed", job.error_code or "required_job_not_completed"
            elif job.status == "succeeded":
                # The handler returned, but no semantic result criterion has
                # been registered. A generated summary is not verified fact.
                state, reason = "inconclusive", "delegated_result_unverified"
            else:
                state, reason = "pending", None
            criteria.append(
                RunCriterionView(
                    kind="delegated_result", source_id=source_id, status=state, reason_code=reason
                )
            )
        else:
            criteria.append(
                RunCriterionView(
                    kind="unsupported_criterion",
                    source_id=source_id,
                    status="inconclusive",
                    reason_code="criterion_not_supported",
                )
            )
    counts = {
        state: sum(item.status == state for item in criteria)
        for state in ("passed", "pending", "failed", "inconclusive")
    }
    status = (
        "not_declared"
        if not criteria
        else "failed"
        if counts["failed"]
        else "pending"
        if counts["pending"]
        else "inconclusive"
        if counts["inconclusive"]
        else "passed"
    )
    return RunGoalView(
        scope="legacy_associations" if legacy else "declared_run_contract",
        status=status,
        required=len(criteria),
        criteria=criteria,
        **counts,
    )
