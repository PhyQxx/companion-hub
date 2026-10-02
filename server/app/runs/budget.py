"""Atomic per-run model reservations, shared across routed provider attempts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select, update

from app.config.models import RunBudgetConfig
from app.db import Database, ModelReservationRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, CallPermit
from app.ids import uuid7
from app.llm.contracts import ModelUsage


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class RunModelBudget:
    def __init__(
        self,
        database: Database,
        *,
        run_id: UUID,
        user_id: UUID,
        config: RunBudgetConfig,
        phase: Literal["interactive", "maintenance"] = "interactive",
    ) -> None:
        self._database = database
        self._run_id = run_id
        self._user_id = user_id
        self._config = config
        self._phase = phase
        self._maintenance_deadline = datetime.now(UTC) + timedelta(
            seconds=config.maintenance_deadline_seconds
        )

    async def reserve(self, *, endpoint: str, tokens: int, final: bool) -> CallPermit:
        if tokens < 1:
            raise ValueError("invalid_budget_reservation")
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            row = await session.scalar(
                select(TaskRunRecord).where(
                    TaskRunRecord.id == self._run_id, TaskRunRecord.user_id == self._user_id
                )
            )
            if row is None:
                raise BudgetDenied("budget_run_not_found")
            if not row.budget or not row.budget.get("enabled"):
                raise BudgetDenied("budget_snapshot_missing")
            max_attempts = min(int(row.budget["max_llm_attempts"]), self._config.max_llm_attempts)
            max_tokens = min(int(row.budget["max_tokens"]), self._config.max_tokens)
            deadline = row.deadline if self._phase == "interactive" else self._maintenance_deadline
            if deadline is None or utc(deadline) <= now:
                raise BudgetDenied("run_deadline_exceeded")
            # Preserve one model-attempt slot for a tool-free interactive answer.
            limit = max_attempts - (self._phase == "interactive" and not final)
            statuses = {"accepted", "running"} if self._phase == "interactive" else {"succeeded"}
            if row.status not in statuses:
                raise BudgetDenied("budget_run_inactive")
            admitted = await session.scalar(
                update(TaskRunRecord)
                .where(
                    TaskRunRecord.id == self._run_id,
                    TaskRunRecord.user_id == self._user_id,
                    TaskRunRecord.status.in_(statuses),
                    TaskRunRecord.llm_attempts < limit,
                    TaskRunRecord.budget_tokens <= max_tokens - tokens,
                )
                .values(
                    llm_attempts=TaskRunRecord.llm_attempts + 1,
                    budget_tokens=TaskRunRecord.budget_tokens + tokens,
                )
                .returning(TaskRunRecord.id)
                .execution_options(synchronize_session=False)
            )
            if admitted is None:
                raise BudgetDenied("run_budget_exhausted")
            call_id = uuid7()
            session.add(
                ModelReservationRecord(
                    call_id=call_id,
                    run_id=self._run_id,
                    phase=self._phase,
                    endpoint=endpoint,
                    state="reserved",
                    reserved_tokens=tokens,
                    charged_tokens=tokens,
                    created_at=now,
                )
            )
        # Include database admission latency in the remaining execution time.
        remaining = (utc(deadline) - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            await self.settle(call_id, None)
            raise BudgetDenied("run_deadline_exceeded")
        return CallPermit(call_id, remaining)

    async def settle(self, call_id: UUID, usage: ModelUsage | None) -> None:
        actual = (
            max(usage.total_tokens, usage.input_tokens + usage.output_tokens)
            if usage is not None and usage.usage_known is not False
            else 0
        )
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.scalar(
                select(ModelReservationRecord)
                .join(TaskRunRecord, TaskRunRecord.id == ModelReservationRecord.run_id)
                .where(
                    ModelReservationRecord.call_id == call_id,
                    ModelReservationRecord.run_id == self._run_id,
                    TaskRunRecord.user_id == self._user_id,
                )
            )
            if record is None:
                # Source deletion also deletes budget records; do not recreate them.
                return
            if actual == 0:
                await session.execute(
                    update(ModelReservationRecord)
                    .where(
                        ModelReservationRecord.call_id == call_id,
                        ModelReservationRecord.state == "reserved",
                    )
                    .values(state="unknown", settled_at=now)
                )
                return
            admitted = await session.scalar(
                update(ModelReservationRecord)
                .where(
                    ModelReservationRecord.call_id == call_id,
                    ModelReservationRecord.state.in_({"reserved", "unknown"}),
                )
                .values(
                    state="settled", actual_tokens=actual, charged_tokens=actual, settled_at=now
                )
                .returning(ModelReservationRecord.call_id)
                .execution_options(synchronize_session=False)
            )
            if admitted is not None:
                await session.execute(
                    update(TaskRunRecord)
                    .where(TaskRunRecord.id == self._run_id)
                    .values(
                        budget_tokens=TaskRunRecord.budget_tokens + actual - record.reserved_tokens
                    )
                )
            else:
                await session.refresh(record)
                if record.actual_tokens != actual:
                    raise BudgetDenied("budget_settlement_conflict")


async def recover_stale_reservations(database: Database) -> None:
    """Preserve charges after a crash. Never release usage we cannot prove unused."""
    async with database.sessions.begin() as session:
        await session.execute(
            update(ModelReservationRecord)
            .where(
                ModelReservationRecord.state == "reserved",
                ModelReservationRecord.created_at < datetime.now(UTC) - timedelta(seconds=1800),
            )
            .values(state="unknown", settled_at=datetime.now(UTC))
        )
