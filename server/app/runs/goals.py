"""Observe registered run criteria conservatively; never infer an unstated business goal."""

from uuid import UUID

from app.db import ActionPlanRecord, ActionStepRecord, JobRecord, TaskRunRecord
from app.schemas.runs import RunCriterionView, RunGoalView

from .outcomes import action_outcome


def goal_view(
    run: TaskRunRecord,
    plans: list[ActionPlanRecord],
    steps: list[ActionStepRecord],
    jobs: list[JobRecord],
) -> RunGoalView:
    criteria: list[RunCriterionView] = []
    root = run.contract.get("criterion")
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
    elif root not in {None, "handler_completed"}:
        criteria.append(
            RunCriterionView(
                kind="unsupported_criterion",
                source_id=run.id,
                status="inconclusive",
                reason_code="criterion_not_supported",
            )
        )
    registered = run.contract.get("required_work")
    # Older runs can report existing links, but do not pretend these were
    # explicitly enrolled when the original contract was accepted.
    legacy = registered is None
    work = (
        registered
        if isinstance(registered, list)
        else [
            *[{"kind": "action_plan", "id": str(plan.id)} for plan in plans],
            *[
                {"kind": "delegated_job", "id": str(job.id)}
                for job in jobs
                if job.kind.startswith("deleg.")
            ],
        ]
    )
    if root == "handler_completed" and not any(
        isinstance(item, dict) and item.get("id") == str(run.id) for item in work
    ):
        work = [*work, {"kind": "delegated_job", "id": str(run.id)}]
    for item in work:
        try:
            source_id = UUID(item["id"])
        except (ValueError, TypeError, KeyError):
            criteria.append(
                RunCriterionView(
                    kind="unsupported_criterion",
                    source_id=run.id,
                    status="inconclusive",
                    reason_code="criterion_invalid",
                )
            )
            continue
        if item.get("kind") == "action_plan":
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
                outcomes = [action_outcome(step) for step in actions]
                if outcomes and all(outcome.validation_status == "passed" for outcome in outcomes):
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
        elif item.get("kind") == "delegated_job":
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
