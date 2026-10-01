from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    ActionPlanRecord,
    ActionStepRecord,
    Database,
    JobRecord,
    TaskRunEventRecord,
    TaskRunRecord,
)
from app.ids import uuid7
from app.schemas.execution import RunActionOutcome
from app.schemas.runs import RunEventView, RunView

from .outcomes import action_outcome

_TRANSITIONS = {
    "accepted": {"running", "failed", "cancelled"},
    "running": {"succeeded", "failed", "cancelled"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}


async def transition_run(session: AsyncSession, run_id: UUID, status: str) -> None:
    row = await session.scalar(
        select(TaskRunRecord).where(TaskRunRecord.id == run_id).with_for_update()
    )
    if row is None or row.status == status:
        return
    if status not in _TRANSITIONS[row.status]:
        return  # Late results must not overwrite terminal cancellation/failure.
    row.status = status
    row.state_version += 1
    if status == "cancelled":
        row.cancel_epoch += 1
    row.updated_at = datetime.now(UTC)
    await append_run_event(session, row, f"run.{status}")


async def append_run_event(session: AsyncSession, row: TaskRunRecord, kind: str) -> None:
    row.event_seq += 1
    session.add(
        TaskRunEventRecord(
            event_id=uuid7(),
            run_id=row.id,
            seq=row.event_seq,
            kind=kind,
            schema_version=1,
            payload={"state_version": row.state_version},
            privacy_level=row.privacy_level,
            occurred_at=datetime.now(UTC),
        )
    )


class RunStore:
    def __init__(self, database: Database) -> None:
        self._database = database

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
            return view.model_copy(
                update={
                    "action_outcomes": [
                        RunActionOutcome(
                            plan_id=step.plan_id,
                            step_id=step.id,
                            outcome=action_outcome(step),
                        )
                        for step in await session.scalars(
                            select(ActionStepRecord)
                            .join(ActionPlanRecord, ActionPlanRecord.id == ActionStepRecord.plan_id)
                            .where(
                                ActionPlanRecord.task_run_id == run_id,
                                ActionPlanRecord.user_id == user_id,
                            )
                            .order_by(ActionStepRecord.plan_id, ActionStepRecord.position)
                        )
                    ],
                    "job_ids": list(
                        await session.scalars(
                            select(JobRecord.id).where(
                                JobRecord.task_run_id == run_id,
                                JobRecord.owner == str(user_id),
                            )
                        )
                    ),
                    "plan_ids": list(
                        await session.scalars(
                            select(ActionPlanRecord.id).where(
                                ActionPlanRecord.task_run_id == run_id,
                                ActionPlanRecord.user_id == user_id,
                            )
                        )
                    ),
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
        return RunView.model_validate(
            {name: getattr(row, name) for name in RunView.model_fields if hasattr(row, name)}
        )
