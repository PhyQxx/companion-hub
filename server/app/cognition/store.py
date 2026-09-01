from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from app.db import (
    ActionResultRecord,
    CognitiveDecisionRecord,
    CognitiveFeedbackRecord,
    CognitiveGoalRecord,
    ConversationRecord,
    Database,
    MessageRecord,
    ReflectionCandidateRecord,
)
from app.ids import uuid7

from .models import (
    ActionResult,
    ClaimedGoalReminder,
    CognitiveDecision,
    CognitiveDecisionView,
    FeedbackKind,
    GoalKind,
    GoalStatus,
    GoalView,
    ReflectionCandidate,
)
from .reflection import FeedbackSummary


def _aware_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class CognitiveStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def save_decision(self, decision: CognitiveDecision) -> None:
        async with self.database.sessions.begin() as session:
            session.add(
                CognitiveDecisionRecord(
                    id=decision.id,
                    user_id=decision.user_id,
                    conversation_id=decision.conversation_id,
                    event_id=decision.event_id,
                    trigger_kind=decision.trigger_kind,
                    decision=str(decision.decision),
                    reason_codes=decision.reason_codes,
                    evidence_ids=decision.evidence_ids,
                    confidence=decision.confidence,
                    urgency=str(decision.urgency),
                    attention_score=decision.attention_score,
                    policy_version=decision.policy_version,
                    model_provider=decision.model_provider,
                    model_name=decision.model_name,
                    expires_at=decision.expires_at,
                    created_at=decision.created_at,
                )
            )

    async def recent_decisions(
        self,
        user_id: UUID,
        *,
        limit: int = 100,
    ) -> list[CognitiveDecisionView]:
        async with self.database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(CognitiveDecisionRecord)
                    .where(CognitiveDecisionRecord.user_id == user_id)
                    .order_by(CognitiveDecisionRecord.created_at.desc())
                    .limit(limit)
                )
            )
        return [
            CognitiveDecisionView(
                id=row.id,
                event_id=row.event_id,
                conversation_id=row.conversation_id,
                trigger_kind=row.trigger_kind,
                decision=row.decision,
                reason_codes=row.reason_codes,
                evidence_ids=row.evidence_ids,
                confidence=row.confidence,
                urgency=row.urgency,
                attention_score=row.attention_score,
                policy_version=row.policy_version,
                model_provider=row.model_provider,
                model_name=row.model_name,
                expires_at=row.expires_at,
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def create_goal(
        self,
        *,
        user_id: UUID,
        kind: GoalKind,
        title: str,
        source_kind: str,
        source_id: str,
        due_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> GoalView:
        if kind in {GoalKind.USER, GoalKind.SHARED} and source_kind not in {
            "message",
            "manual",
        }:
            raise ValueError("user/shared goals require explicit user or manual evidence")
        if source_kind == "message":
            try:
                message_id = UUID(source_id)
            except ValueError as error:
                raise ValueError("goal message evidence must be a valid message id") from error
            async with self.database.sessions() as session:
                evidence = await session.scalar(
                    select(MessageRecord.id)
                    .join(
                        ConversationRecord,
                        ConversationRecord.id == MessageRecord.conversation_id,
                    )
                    .where(
                        MessageRecord.id == message_id,
                        ConversationRecord.user_id == user_id,
                    )
                )
            if evidence is None:
                raise ValueError("goal message evidence does not belong to the user")
        now = datetime.now(UTC)
        record = CognitiveGoalRecord(
            id=uuid7(),
            user_id=user_id,
            kind=kind.value,
            title=title.strip(),
            status=GoalStatus.ACTIVE.value,
            source_kind=source_kind,
            source_id=source_id,
            due_at=due_at,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        async with self.database.sessions.begin() as session:
            session.add(record)
        return _goal(record)

    async def goal_by_source(
        self,
        user_id: UUID,
        *,
        source_kind: str,
        source_id: str,
    ) -> GoalView | None:
        """按证据定位目标；承诺提取用它做同一消息的幂等去重。"""
        async with self.database.sessions() as session:
            record = await session.scalar(
                select(CognitiveGoalRecord).where(
                    CognitiveGoalRecord.user_id == user_id,
                    CognitiveGoalRecord.source_kind == source_kind,
                    CognitiveGoalRecord.source_id == source_id,
                )
            )
        return _goal(record) if record is not None else None

    async def set_goal_status(
        self,
        *,
        user_id: UUID,
        goal_id: UUID,
        status: GoalStatus,
    ) -> GoalView:
        if status not in {GoalStatus.COMPLETED, GoalStatus.CANCELLED}:
            raise ValueError("goal status can only be completed or cancelled explicitly")
        async with self.database.sessions.begin() as session:
            record = await session.get(CognitiveGoalRecord, goal_id)
            if record is None or record.user_id != user_id:
                raise LookupError("cognitive goal not found")
            if record.status != GoalStatus.ACTIVE.value:
                raise ValueError("only active goals can change status")
            record.status = status.value
            record.updated_at = datetime.now(UTC)
        return _goal(record)

    async def active_goals(self, user_id: UUID, *, now: datetime) -> list[GoalView]:
        async with self.database.sessions.begin() as session:
            expired = list(
                await session.scalars(
                    select(CognitiveGoalRecord).where(
                        CognitiveGoalRecord.user_id == user_id,
                        CognitiveGoalRecord.status == GoalStatus.ACTIVE.value,
                        CognitiveGoalRecord.expires_at.is_not(None),
                        CognitiveGoalRecord.expires_at <= now,
                    )
                )
            )
            for item in expired:
                item.status = GoalStatus.EXPIRED.value
                item.updated_at = now
            rows = list(
                await session.scalars(
                    select(CognitiveGoalRecord)
                    .where(
                        CognitiveGoalRecord.user_id == user_id,
                        CognitiveGoalRecord.status == GoalStatus.ACTIVE.value,
                    )
                    .order_by(CognitiveGoalRecord.due_at, CognitiveGoalRecord.created_at)
                    .limit(16)
                )
            )
        return [_goal(row) for row in rows]

    async def claim_due_goal_reminders(
        self,
        *,
        now: datetime | None = None,
        pre_due_window: timedelta = timedelta(hours=24),
        limit: int = 20,
    ) -> list[ClaimedGoalReminder]:
        """认领到期的目标提醒（跨用户，全局调度）。

        pre_due：到期前窗口内提醒一次；due：到期后提醒一次。两者都受
        reminder_defer_until 推迟闸门约束。时间戳列即乐观守卫——并发
        认领只有一方能把 NULL 更新为非空。
        """
        moment = now or datetime.now(UTC)
        claimed: list[ClaimedGoalReminder] = []
        async with self.database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(CognitiveGoalRecord)
                    .where(
                        CognitiveGoalRecord.status == GoalStatus.ACTIVE.value,
                        CognitiveGoalRecord.due_at.is_not(None),
                    )
                    .order_by(CognitiveGoalRecord.due_at)
                    .limit(limit)
                )
            )
        for record in rows:
            defer_until = _aware_or_none(record.reminder_defer_until)
            if defer_until is not None and defer_until > moment:
                continue
            due_at = _aware_or_none(record.due_at)
            assert due_at is not None  # 上面已过滤非空
            if due_at <= moment:
                phase: Literal["pre_due", "due"] = "due"
            elif moment >= due_at - pre_due_window:
                phase = "pre_due"
            else:
                continue
            column = (
                CognitiveGoalRecord.due_reminded_at
                if phase == "due"
                else CognitiveGoalRecord.pre_due_reminded_at
            )
            async with self.database.sessions.begin() as write:
                result = await write.execute(
                    update(CognitiveGoalRecord)
                    .where(
                        CognitiveGoalRecord.id == record.id,
                        CognitiveGoalRecord.status == GoalStatus.ACTIVE.value,
                        column.is_(None),
                    )
                    .values({column: moment, "updated_at": moment})
                )
                if not int(cast(CursorResult[Any], result).rowcount or 0):
                    continue
            claimed.append(
                ClaimedGoalReminder(
                    user_id=record.user_id,
                    goal=_goal(record),
                    phase=phase,
                    due_at=due_at,
                )
            )
        return claimed

    async def defer_goal_reminders(
        self,
        user_id: UUID,
        goal_id: UUID,
        *,
        until: datetime,
        now: datetime | None = None,
    ) -> GoalView:
        """稍后提醒：推迟该目标的全部未发提醒。"""
        moment = now or datetime.now(UTC)
        if until <= moment:
            raise ValueError("推迟时间必须在未来")
        async with self.database.sessions.begin() as session:
            record = await session.get(CognitiveGoalRecord, goal_id)
            if record is None or record.user_id != user_id:
                raise LookupError("cognitive goal not found")
            if record.status != GoalStatus.ACTIVE.value:
                raise ValueError("only active goals can defer reminders")
            record.reminder_defer_until = until
            record.updated_at = moment
        return _goal(record)

    async def ignore_goal_reminder(
        self,
        user_id: UUID,
        goal_id: UUID,
        *,
        now: datetime | None = None,
    ) -> GoalView:
        """忽略降频：记录忽略并顺延一天，重复忽略保持每天最多打扰一次。"""
        moment = now or datetime.now(UTC)
        async with self.database.sessions.begin() as session:
            record = await session.get(CognitiveGoalRecord, goal_id)
            if record is None or record.user_id != user_id:
                raise LookupError("cognitive goal not found")
            if record.status != GoalStatus.ACTIVE.value:
                raise ValueError("only active goals can ignore reminders")
            record.ignored_count += 1
            record.reminder_defer_until = moment + timedelta(days=1)
            record.updated_at = moment
        return _goal(record)

    async def add_feedback(
        self,
        *,
        user_id: UUID,
        decision_id: UUID,
        kind: FeedbackKind,
    ) -> UUID:
        async with self.database.sessions() as session:
            decision = await session.get(CognitiveDecisionRecord, decision_id)
        if decision is None or decision.user_id != user_id:
            raise LookupError("cognitive decision not found")
        feedback_id = uuid7()
        async with self.database.sessions.begin() as session:
            session.add(
                CognitiveFeedbackRecord(
                    id=feedback_id,
                    user_id=user_id,
                    decision_id=decision_id,
                    kind=kind.value,
                    metadata_json={},
                    created_at=datetime.now(UTC),
                )
            )
        return feedback_id

    async def reflection_candidate(
        self, *, user_id: UUID, trigger_kind: str
    ) -> ReflectionCandidate | None:
        async with self.database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(CognitiveFeedbackRecord)
                    .join(
                        CognitiveDecisionRecord,
                        CognitiveDecisionRecord.id == CognitiveFeedbackRecord.decision_id,
                    )
                    .where(
                        CognitiveFeedbackRecord.user_id == user_id,
                        CognitiveDecisionRecord.trigger_kind == trigger_kind,
                        CognitiveFeedbackRecord.kind.in_(
                            [FeedbackKind.IGNORED.value, FeedbackKind.FORBIDDEN.value]
                        ),
                    )
                    .order_by(CognitiveFeedbackRecord.created_at.desc())
                    .limit(3)
                )
            )
        if len(rows) < 3:
            return None
        return ReflectionCandidate(
            content=f"用户对 {trigger_kind} 类主动提醒的接受度较低，应降低频率。",
            evidence_ids=[str(item.id) for item in rows],
            confidence=0.8,
            requires_confirmation=True,
        )

    # ----- Action Results -----

    async def save_action_result(self, result: ActionResult, *, user_id: UUID) -> None:
        async with self.database.sessions.begin() as session:
            session.add(
                ActionResultRecord(
                    id=uuid7(),
                    user_id=user_id,
                    decision_id=result.decision_id,
                    level="A0",  # simplified; caller should derive from plan if needed
                    outcome=str(result.outcome),
                    reason_code=result.reason_code,
                    verified=result.verified,
                    observed_state=result.observed_state,
                    created_at=datetime.now(UTC),
                )
            )

    async def recent_action_results(self, user_id: UUID, *, limit: int = 100) -> list[ActionResult]:
        async with self.database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(ActionResultRecord)
                    .where(ActionResultRecord.user_id == user_id)
                    .order_by(ActionResultRecord.created_at.desc())
                    .limit(limit)
                )
            )
        return [
            ActionResult(
                decision_id=row.decision_id,
                outcome=row.outcome,
                reason_code=row.reason_code,
                verified=row.verified,
                observed_state=row.observed_state,
            )
            for row in rows
        ]

    # ----- Reflection Store Protocol -----

    async def distinct_trigger_kinds(self, user_id: UUID, *, since: datetime) -> list[str]:
        async with self.database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(CognitiveDecisionRecord.trigger_kind)
                    .join(
                        CognitiveFeedbackRecord,
                        CognitiveFeedbackRecord.decision_id == CognitiveDecisionRecord.id,
                    )
                    .where(
                        CognitiveDecisionRecord.user_id == user_id,
                        CognitiveFeedbackRecord.created_at >= since,
                    )
                    .distinct()
                )
            )
        return [str(r) for r in rows]

    async def feedback_summary(
        self,
        *,
        user_id: UUID,
        trigger_kind: str,
        since: datetime,
    ) -> FeedbackSummary:
        async with self.database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(CognitiveFeedbackRecord)
                    .join(
                        CognitiveDecisionRecord,
                        CognitiveDecisionRecord.id == CognitiveFeedbackRecord.decision_id,
                    )
                    .where(
                        CognitiveDecisionRecord.user_id == user_id,
                        CognitiveDecisionRecord.trigger_kind == trigger_kind,
                        CognitiveFeedbackRecord.created_at >= since,
                    )
                )
            )
        counts: dict[str, int] = {"accepted": 0, "ignored": 0, "snoozed": 0, "forbidden": 0}
        for row in rows:
            kind = row.kind
            if kind in counts:
                counts[kind] += 1
        total = len(rows)
        acceptance_rate = counts["accepted"] / total if total else 0.0
        return FeedbackSummary(
            total=total,
            accepted=counts["accepted"],
            ignored=counts["ignored"],
            snoozed=counts["snoozed"],
            forbidden=counts["forbidden"],
            acceptance_rate=acceptance_rate,
        )

    async def save_candidate(
        self,
        *,
        user_id: UUID,
        candidate: ReflectionCandidate,
    ) -> UUID:
        candidate_id = uuid7()
        async with self.database.sessions.begin() as session:
            session.add(
                ReflectionCandidateRecord(
                    id=candidate_id,
                    user_id=user_id,
                    content=candidate.content,
                    evidence_ids=candidate.evidence_ids,
                    confidence=candidate.confidence,
                    requires_confirmation=candidate.requires_confirmation,
                    status="pending",
                    created_at=datetime.now(UTC),
                )
            )
        return candidate_id

    async def pending_candidates(
        self, user_id: UUID, *, limit: int = 50
    ) -> list[ReflectionCandidate]:
        async with self.database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(ReflectionCandidateRecord)
                    .where(
                        ReflectionCandidateRecord.user_id == user_id,
                        ReflectionCandidateRecord.status == "pending",
                    )
                    .order_by(ReflectionCandidateRecord.created_at.desc())
                    .limit(limit)
                )
            )
        return [
            ReflectionCandidate(
                content=row.content,
                evidence_ids=row.evidence_ids,
                confidence=row.confidence,
                requires_confirmation=row.requires_confirmation,
            )
            for row in rows
        ]


def _goal(record: CognitiveGoalRecord) -> GoalView:
    return GoalView(
        id=record.id,
        kind=record.kind,
        title=record.title,
        status=record.status,
        source_kind=record.source_kind,
        source_id=record.source_id,
        due_at=record.due_at,
        expires_at=record.expires_at,
        pre_due_reminded_at=record.pre_due_reminded_at,
        due_reminded_at=record.due_reminded_at,
        reminder_defer_until=record.reminder_defer_until,
        ignored_count=record.ignored_count,
    )
