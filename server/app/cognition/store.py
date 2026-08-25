# ruff: noqa: RUF001
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.db import (
    CognitiveDecisionRecord,
    CognitiveFeedbackRecord,
    CognitiveGoalRecord,
    ConversationRecord,
    Database,
    MessageRecord,
)
from app.ids import uuid7

from .models import (
    CognitiveDecision,
    CognitiveDecisionView,
    FeedbackKind,
    GoalKind,
    GoalStatus,
    GoalView,
    ReflectionCandidate,
)


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
    )
