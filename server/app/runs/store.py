from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.db import (
    ActionPlanRecord,
    ActionStepRecord,
    ConversationRecord,
    Database,
    InteractionTurnRecord,
    JobRecord,
    ModelReservationRecord,
    TaskRunEventRecord,
    TaskRunRecord,
)
from app.db.claims import lock_job
from app.ids import uuid7
from app.schemas.billing import BillingEvidence, BillingReport, CostPeriod, CostSnapshot
from app.schemas.costs import CostSummaryView
from app.schemas.execution import RunActionOutcome
from app.schemas.runs import RunBudgetView, RunEventView, RunView

from .goals import goal_view
from .outcomes import action_outcome

_TRANSITIONS = {
    "accepted": {"running", "failed", "cancelled"},
    "running": {"succeeded", "failed", "cancelled"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}


async def transition_run(session: AsyncSession, run_id: UUID, status: str) -> None:
    sources = {source for source, targets in _TRANSITIONS.items() if status in targets}
    if not sources:
        return
    # New runs and the caller's already-fenced metadata belong to this same UoW.
    # The conditional write then decides from current committed state, including
    # after waiting for another writer; cached identities are not authorities.
    await session.flush()
    row = await session.scalar(
        update(TaskRunRecord)
        .where(TaskRunRecord.id == run_id, TaskRunRecord.status.in_(sources))
        .values(
            status=status,
            state_version=TaskRunRecord.state_version + 1,
            cancel_epoch=(
                TaskRunRecord.cancel_epoch + 1
                if status == "cancelled"
                else TaskRunRecord.cancel_epoch
            ),
            updated_at=datetime.now(UTC),
        )
        .returning(TaskRunRecord)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    if row is None:
        return  # Late results cannot overwrite an already terminal run.
    await append_run_event(session, row, f"run.{status}")


async def append_run_event(
    session: AsyncSession,
    row: TaskRunRecord,
    kind: str,
    *,
    payload: dict[str, object] | None = None,
) -> None:
    await session.flush()
    allocation = (
        await session.execute(
            update(TaskRunRecord)
            .where(TaskRunRecord.id == row.id)
            .values(event_seq=TaskRunRecord.event_seq + 1)
            .returning(
                TaskRunRecord.event_seq, TaskRunRecord.state_version, TaskRunRecord.privacy_level
            )
            .execution_options(synchronize_session=False)
        )
    ).one_or_none()
    if allocation is None:
        raise LookupError("run not found")
    # Do not turn this allocation into a later stale ORM flush UPDATE.
    set_committed_value(row, "event_seq", allocation.event_seq)
    session.add(
        TaskRunEventRecord(
            event_id=uuid7(),
            run_id=row.id,
            seq=allocation.event_seq,
            kind=kind,
            schema_version=1,
            payload={**(payload or {}), "state_version": allocation.state_version},
            privacy_level=allocation.privacy_level,
            occurred_at=datetime.now(UTC),
        )
    )


class RunStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def cost_summary(self, *, user_id: UUID, days: int = 30) -> CostSummaryView:
        from .costs import cost_summary

        return await cost_summary(self._database, user_id=user_id, days=days)

    async def cost_snapshot(self, *, user_id: UUID, period: CostPeriod) -> CostSnapshot:
        from .billing import cost_snapshot

        return await cost_snapshot(self._database, user_id=user_id, period=period)

    async def reconcile_bill(self, *, user_id: UUID, evidence: BillingEvidence) -> BillingReport:
        from .billing import reconcile_bill

        return await reconcile_bill(self._database, user_id=user_id, evidence=evidence)

    async def cancel_background(self, run_id: UUID, *, user_id: UUID) -> bool:
        async with self._database.sessions.begin() as session:
            # Admission and completion lock Job before Run. Cancellation must
            # use the same order so a model reservation cannot deadlock it.
            # The owned root Job is also the first SQLite write; an empty
            # match still serializes roots without a linked Job before reads.
            await session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == run_id,
                    JobRecord.task_run_id == run_id,
                    JobRecord.owner == str(user_id),
                )
                .values(progress=JobRecord.progress)
                .execution_options(synchronize_session=False)
            )
            await session.scalars(
                select(JobRecord)
                .where(
                    JobRecord.task_run_id == run_id,
                    JobRecord.owner == str(user_id),
                )
                .order_by(JobRecord.id)
                .with_for_update()
            )
            row = await session.scalar(
                update(TaskRunRecord)
                .where(
                    TaskRunRecord.id == run_id,
                    TaskRunRecord.user_id == user_id,
                )
                .values(updated_at=TaskRunRecord.updated_at)
                .returning(TaskRunRecord)
                .execution_options(synchronize_session=False, populate_existing=True)
            )
            if row is None:
                raise LookupError("run not found")
            if row.contract.get("entry") not in {"delegated", "background"} or row.status not in {
                "accepted",
                "running",
            }:
                return False
            now = datetime.now(UTC)
            # Immediate run cancellation blocks future model admission. Running
            # jobs acknowledge cancellation after their external operation returns.
            await session.execute(
                update(JobRecord)
                .where(
                    JobRecord.task_run_id == run_id,
                    JobRecord.owner == str(user_id),
                    JobRecord.status.in_({"running", "admitted"}),
                )
                .values(status="cancelling", cancel_requested_at=now)
            )
            await session.execute(
                update(JobRecord)
                .where(
                    JobRecord.task_run_id == run_id,
                    JobRecord.owner == str(user_id),
                    JobRecord.status.in_({"queued", "retry_wait", "waiting_user"}),
                )
                .values(
                    status="cancelled",
                    cancel_requested_at=now,
                    completed_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            await transition_run(session, run_id, "cancelled")
            return True

    async def cancel_work(
        self,
        run_id: UUID,
        *,
        user_id: UUID,
        reason: str = "run_cancelled",
    ) -> tuple[bool, tuple[UUID, ...]]:
        async with self._database.sessions.begin() as session:
            # Fence the owned conversation in the first write, before locating
            # its Run. An empty match still prevents SQLite read upgrades for
            # roots without a conversation. Preserve conversation -> Job -> Run.
            source_id = (
                select(TaskRunRecord.conversation_id)
                .where(TaskRunRecord.id == run_id, TaskRunRecord.user_id == user_id)
                .scalar_subquery()
            )
            conversation = await session.scalar(
                update(ConversationRecord)
                .where(ConversationRecord.id == source_id, ConversationRecord.user_id == user_id)
                .values(id=ConversationRecord.id)
                .returning(ConversationRecord.id)
                .execution_options(synchronize_session=False)
            )
            row = await session.get(TaskRunRecord, run_id)
            if row is None or row.user_id != user_id:
                raise LookupError("run not found")
            if row.conversation_id != conversation:
                raise LookupError("run not found")
            if row.conversation_id is None:
                await lock_job(session, run_id)
            job_ids = list(
                await session.scalars(
                    select(JobRecord.id)
                    .where(
                        JobRecord.task_run_id == run_id,
                        JobRecord.owner == str(user_id),
                        (JobRecord.kind.like("deleg.%") | (JobRecord.id == run_id)),
                    )
                    .order_by(JobRecord.id)
                    .with_for_update()
                )
            )
            jobs = []
            for job_id in job_ids:
                job = await lock_job(session, job_id)
                if job is not None and job.task_run_id == run_id and job.owner == str(user_id):
                    jobs.append(job)
            plans = list(
                await session.scalars(
                    select(ActionPlanRecord)
                    .where(
                        ActionPlanRecord.task_run_id == run_id,
                        ActionPlanRecord.user_id == user_id,
                    )
                    .order_by(ActionPlanRecord.id)
                    .with_for_update()
                )
            )
            now = datetime.now(UTC)
            changed = False
            for job in jobs:
                if job.status in {"queued", "retry_wait", "waiting_user"}:
                    job.status = "cancelled"
                    job.cancel_requested_at = job.completed_at = now
                    job.lease_owner = job.lease_expires_at = None
                    changed = True
                elif job.status in {"running", "admitted"}:
                    job.status = "cancelling"
                    job.cancel_requested_at = now
                    changed = True
            for plan in plans:
                if plan.status not in {"ready", "awaiting_confirmation", "executing"}:
                    continue
                changed = changed or not plan.cancel_requested
                plan.cancel_requested = True
                plan.cancel_reason = "run_cancelled"
                plan.reason_code = "run_cancelled"
                plan.updated_at = now
                if plan.status != "executing":
                    plan.status = "cancelled"
                    plan.cancelled_at = now
                await session.execute(
                    update(ActionStepRecord)
                    .where(
                        ActionStepRecord.plan_id == plan.id,
                        ActionStepRecord.status.in_({"ready", "awaiting_confirmation"}),
                    )
                    .values(status="cancelled", reason_code="run_cancelled", completed_at=now)
                )
            generations: list[UUID] = []
            for turn in await session.scalars(
                select(InteractionTurnRecord)
                .where(
                    InteractionTurnRecord.task_run_id == run_id,
                    InteractionTurnRecord.state.in_({"accepted", "thinking", "streaming"}),
                )
                .with_for_update()
            ):
                turn.state = "cancelled"
                turn.state_version += 1
                turn.cancel_reason = reason[:160]
                turn.completed_at = now
                generations.append(turn.generation_id)
            # Ownership can change while waiting on earlier locks. Scope the
            # final write fence too; failure rolls back all dependent updates.
            row = await session.scalar(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == run_id, TaskRunRecord.user_id == user_id)
                .values(updated_at=TaskRunRecord.updated_at)
                .returning(TaskRunRecord)
                .execution_options(synchronize_session=False, populate_existing=True)
            )
            if row is None:
                raise LookupError("run not found")
            if changed or row.status in {"accepted", "running"}:
                row.contract = {**row.contract, "work_cancel_requested": True}
            if row.status in {"accepted", "running"}:
                await transition_run(session, run_id, "cancelled")
                changed = True
            elif changed:
                row.cancel_epoch += 1
                row.state_version += 1
                row.updated_at = now
                await append_run_event(session, row, "run.work.cancel_requested")
            return changed, tuple(generations)

    async def get(self, run_id: UUID, *, user_id: UUID) -> RunView:
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(TaskRunRecord).where(
                    TaskRunRecord.id == run_id,
                    TaskRunRecord.user_id == user_id,
                )
            )
            if row is None:
                raise LookupError("run not found")
            view = self._view(row)
            if view.budget_summary is not None:
                counts = {
                    state: count
                    for state, count in (
                        await session.execute(
                            select(ModelReservationRecord.state, func.count())
                            .where(ModelReservationRecord.run_id == run_id)
                            .group_by(ModelReservationRecord.state)
                        )
                    ).all()
                }
                view = view.model_copy(
                    update={
                        "budget_summary": view.budget_summary.model_copy(
                            update={
                                "unsettled_calls": counts.get("reserved", 0),
                                "unknown_usage_calls": counts.get("unknown", 0),
                            }
                        )
                    }
                )
            plans = list(
                await session.scalars(
                    select(ActionPlanRecord)
                    .where(
                        ActionPlanRecord.task_run_id == run_id,
                        ActionPlanRecord.user_id == user_id,
                    )
                    .order_by(ActionPlanRecord.id)
                )
            )
            steps = list(
                await session.scalars(
                    select(ActionStepRecord)
                    .join(
                        ActionPlanRecord,
                        ActionPlanRecord.id == ActionStepRecord.plan_id,
                    )
                    .where(
                        ActionPlanRecord.task_run_id == run_id, ActionPlanRecord.user_id == user_id
                    )
                    .order_by(ActionStepRecord.plan_id, ActionStepRecord.position)
                )
            )
            jobs = list(
                await session.scalars(
                    select(JobRecord)
                    .where(
                        JobRecord.task_run_id == run_id,
                        JobRecord.owner == str(user_id),
                    )
                    .order_by(JobRecord.id)
                )
            )
            return view.model_copy(
                update={
                    "action_outcomes": [
                        RunActionOutcome(
                            plan_id=step.plan_id, step_id=step.id, outcome=action_outcome(step)
                        )
                        for step in steps
                    ],
                    "job_ids": [job.id for job in jobs],
                    "plan_ids": [plan.id for plan in plans],
                    "goal": goal_view(row, plans, steps, jobs),
                }
            )

    async def list_runs(
        self, *, user_id: UUID, before_id: UUID | None = None, limit: int = 50
    ) -> list[RunView]:
        query = select(TaskRunRecord).where(TaskRunRecord.user_id == user_id)
        if before_id is not None:
            query = query.where(TaskRunRecord.id < before_id)
        async with self._database.sessions() as session:
            return [
                self._view(row)
                for row in await session.scalars(
                    query.order_by(TaskRunRecord.id.desc()).limit(min(max(limit, 1), 100)),
                )
            ]

    async def events(
        self, run_id: UUID, *, user_id: UUID, after_seq: int = 0
    ) -> list[RunEventView]:
        async with self._database.sessions() as session:
            owned = await session.scalar(
                select(TaskRunRecord.id).where(
                    TaskRunRecord.id == run_id,
                    TaskRunRecord.user_id == user_id,
                )
            )
            if owned is None:
                raise LookupError("run not found")
            rows = await session.scalars(
                select(TaskRunEventRecord)
                .where(
                    TaskRunEventRecord.run_id == run_id,
                    TaskRunEventRecord.seq > after_seq,
                )
                .order_by(TaskRunEventRecord.seq)
                .limit(200)
            )
            return [
                RunEventView.model_validate(
                    {name: getattr(row, name) for name in RunEventView.model_fields}
                )
                for row in rows
            ]

    @staticmethod
    def _view(row: TaskRunRecord) -> RunView:
        data = {name: getattr(row, name) for name in RunView.model_fields if hasattr(row, name)}
        if row.budget:
            usage = row.contract.get("resource_usage", {})
            usage = usage if isinstance(usage, dict) else {}
            attempts = int(usage.get("tool_attempts", 0))
            unknown = int(usage.get("unknown_tool_calls", 0))
            returned = int(usage.get("returned_tool_calls", 0))
            data["budget_summary"] = RunBudgetView(
                max_tool_attempts=int(row.budget.get("max_tool_attempts", 64)),
                tool_attempts=attempts,
                unknown_tool_calls=unknown,
                unsettled_tool_calls=max(0, attempts - unknown - returned),
                enabled=bool(row.budget["enabled"]),
                max_llm_attempts=int(row.budget["max_llm_attempts"]),
                max_tokens=int(row.budget["max_tokens"]),
                llm_attempts=row.llm_attempts,
                charged_tokens=row.budget_tokens,
            )
        return RunView.model_validate(data)
