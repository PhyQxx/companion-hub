"""SQL source repository for bounded delivery; domain rows stay behind this port."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import CognitiveGoalRecord, DailyBriefRecord, DailyReviewRecord, TaskItemRecord
from app.harness.budget import BudgetDenied
from app.harness.time import utc
from app.schemas.common import PrivacyLevel

from .delivery_contracts import DeliverySourceRepository as DeliverySourceRepository
from .delivery_contracts import DeliverySourceSnapshot, GoalDeliveryClaim

SourceTable = (
    type[DailyBriefRecord]
    | type[DailyReviewRecord]
    | type[TaskItemRecord]
    | type[CognitiveGoalRecord]
)
SourceRow = DailyBriefRecord | DailyReviewRecord | TaskItemRecord | CognitiveGoalRecord


def goal_delivery_text(title: str, due_at: datetime, claim: GoalDeliveryClaim) -> str:
    due_text = utc(due_at).astimezone(ZoneInfo(claim.timezone)).strftime("%m-%d %H:%M")
    if claim.phase == "pre_due":
        return f"📌 你有一个目标临近：{title}（预计 {due_text} 到期）"
    return f"⏰ 目标已到期：{title}（{due_text}）"


async def _source_lock(
    session: AsyncSession,
    table: SourceTable,
    source_id: UUID,
    user_id: UUID,
    *,
    pending: bool = False,
) -> SourceRow | None:
    query = update(table).where(table.id == source_id, table.user_id == user_id)
    if pending:
        query = query.where(
            table.status.in_({"active", "firing"})
            if table is TaskItemRecord
            else table.status == "active"
            if table is CognitiveGoalRecord
            else table.status == "pending"
        )
    return cast(
        SourceRow | None,
        await session.scalar(query.values(updated_at=table.updated_at).returning(table)),
    )


async def _check_goal_sources(session: AsyncSession, source: SourceRow) -> None:
    from app.cognition.goal_sources import goal_visibility

    if isinstance(source, TaskItemRecord):
        return
    items = (
        source.facts
        if isinstance(source, DailyBriefRecord)
        else source.items
        if isinstance(source, DailyReviewRecord)
        else []
    )
    identifiers: set[UUID] = {source.id} if isinstance(source, CognitiveGoalRecord) else set()
    for item in items:
        if item.get("action") == "removed":
            continue
        reference = item.get("source", "")
        if isinstance(reference, str) and reference.startswith("goal:"):
            try:
                identifiers.add(UUID(reference[5:]))
            except ValueError as error:
                raise BudgetDenied("delivery_source_invalid") from error
    if not identifiers:
        return
    if len(identifiers) > 1000:
        raise BudgetDenied("delivery_source_invalid")
    allowed = set(
        await session.scalars(
            select(CognitiveGoalRecord.id).where(
                CognitiveGoalRecord.id.in_(identifiers),
                CognitiveGoalRecord.user_id == source.user_id,
                goal_visibility(PrivacyLevel.L1),
            )
        )
    )
    if allowed != identifiers:
        raise BudgetDenied("delivery_source_private_or_deleted")


def task_delivery_text(title: str, notes: str | None) -> str:
    return f"⏰ 提醒：{title}" + (f"\n{notes}" if notes else "")


def _matches(
    source: SourceRow | None,
    fingerprint: str,
    run_id: UUID,
    goal_claim: GoalDeliveryClaim | None = None,
) -> bool:
    if source is None:
        return False
    if isinstance(source, TaskItemRecord):
        if source.status not in {"active", "firing"} or source.source == "pnkx":
            return False
        if (source.last_delivery or {}).get("run_id") != str(run_id):
            return False
        text = task_delivery_text(source.title, source.notes)
    elif isinstance(source, CognitiveGoalRecord):
        if goal_claim is None or source.status != "active" or source.due_at is None:
            return False
        stamp = source.due_reminded_at if goal_claim.phase == "due" else source.pre_due_reminded_at
        if stamp is None or utc(stamp) != utc(goal_claim.claimed_at):
            return False
        if source.reminder_defer_until and utc(source.reminder_defer_until) > utc(
            goal_claim.claimed_at
        ):
            return False
        if source.expires_at and utc(source.expires_at) <= datetime.now(UTC):
            return False
        text = goal_delivery_text(source.title, source.due_at, goal_claim)
    else:
        text = source.text
    return hashlib.sha256(text.encode()).hexdigest() == fingerprint


@dataclass(frozen=True)
class SqlDeliverySourceRepository:
    table: SourceTable
    source_id: UUID
    user_id: UUID
    goal_claim: GoalDeliveryClaim | None = None

    def __post_init__(self) -> None:
        if self.table is CognitiveGoalRecord:
            if self.goal_claim is None or self.goal_claim.phase not in {"due", "pre_due"}:
                raise BudgetDenied("delivery_source_invalid")
        elif self.goal_claim is not None:
            raise BudgetDenied("delivery_source_invalid")

    def request_key(self, entry: str, run_id: UUID) -> str:
        if self.goal_claim is not None:
            return f"{entry}:{self.source_id}:{utc(self.goal_claim.claimed_at).isoformat()}"
        return f"{entry}:{run_id}"

    async def lock(self, session: AsyncSession, *, pending: bool = False) -> bool:
        return (
            await _source_lock(session, self.table, self.source_id, self.user_id, pending=pending)
            is not None
        )

    async def inspect(
        self, session: AsyncSession, fingerprint: str, run_id: UUID
    ) -> DeliverySourceSnapshot:
        source = cast(SourceRow | None, await session.get(self.table, self.source_id))
        if (
            source is None
            or source.user_id != self.user_id
            or not _matches(source, fingerprint, run_id, self.goal_claim)
        ):
            raise BudgetDenied("delivery_source_changed")
        await _check_goal_sources(session, source)
        return DeliverySourceSnapshot(
            privacy_level=source.privacy_level if isinstance(source, TaskItemRecord) else "L1",
            generation=source.fire_count if isinstance(source, TaskItemRecord) else None,
            goal_claim=self.goal_claim,
        )

    async def finish(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        entry: str,
        channels: list[str],
        reason: str | None,
        state: str,
        now: datetime,
    ) -> bool:
        source = cast(SourceRow | None, await session.get(self.table, self.source_id))
        if source is None or source.user_id != self.user_id:
            return False
        cancelled = False
        if isinstance(source, TaskItemRecord) and (source.last_delivery or {}).get("run_id") == str(
            run_id
        ):
            source.last_delivery = {
                "run_id": str(run_id),
                "fire_count": source.fire_count,
                "trigger_kind": entry,
                "fired_at": utc(source.last_fired_at).isoformat() if source.last_fired_at else None,
                "channels": channels,
                "outcome": state,
                "reason_code": f"deliver_error:{reason}"
                if reason and reason.endswith("Error")
                else reason,
            }
            source.updated_at = now
            cancelled = source.status == "cancelled"
            if source.status == "firing":
                source.status, source.completed_at, source.next_fire_at = "done", now, None
        elif isinstance(source, (DailyBriefRecord, DailyReviewRecord)) and state != "not_started":
            # Compatibility: delivered historically means one terminal attempt.
            source.status, source.delivered_at, source.channels, source.updated_at = (
                "delivered",
                now,
                channels,
                now,
            )
        return cancelled
