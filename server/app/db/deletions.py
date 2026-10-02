"""Shared source deletion cascade for live deletion and offline restore replay."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    ActionPlanRecord,
    ActionStepRecord,
    CognitiveGoalRecord,
    ConversationRecord,
    InteractionTurnRecord,
    JobRecord,
    MessageRecord,
    ModelReservationRecord,
    SkillDraftRecord,
    TaskRunEventRecord,
    TaskRunRecord,
    TimelineEventRecord,
    WorkflowDraftRecord,
)


async def purge_conversation(
    session: AsyncSession,
    conversation_id: UUID,
    *,
    user_id: UUID,
) -> bool:
    """Caller owns the transaction; never enqueue work or call an external provider."""
    conversation = await session.scalar(
        select(ConversationRecord).where(ConversationRecord.id == conversation_id).with_for_update()
    )
    if conversation is None or conversation.user_id != user_id:
        return False
    message_ids = [
        str(value)
        for value in await session.scalars(
            select(MessageRecord.id).where(MessageRecord.conversation_id == conversation_id)
        )
    ]
    turn_ids = list(
        await session.scalars(
            select(InteractionTurnRecord.id).where(
                InteractionTurnRecord.conversation_id == conversation_id,
            )
        )
    )
    run_ids = list(
        await session.scalars(
            select(TaskRunRecord.id).where(
                TaskRunRecord.conversation_id == conversation_id,
            )
        )
    )
    # Derived writes lock Conversation -> Job -> derived rows. Take
    # the same order before purging candidates so cancellation and
    # deletion cannot deadlock a worker committing its evidence.
    if run_ids:
        await session.scalars(
            select(JobRecord)
            .where(JobRecord.task_run_id.in_(run_ids))
            .order_by(JobRecord.id)
            .with_for_update()
        )
    if turn_ids:
        await session.execute(
            delete(SkillDraftRecord).where(
                SkillDraftRecord.turn_id.in_([str(turn_id) for turn_id in turn_ids]),
            )
        )
    await session.execute(
        delete(CognitiveGoalRecord).where(
            CognitiveGoalRecord.user_id == user_id,
            CognitiveGoalRecord.source_kind == "message",
            CognitiveGoalRecord.source_id.in_(message_ids),
        )
    )
    await session.execute(
        delete(InteractionTurnRecord).where(
            InteractionTurnRecord.conversation_id == conversation_id
        )
    )
    await session.execute(
        delete(MessageRecord).where(MessageRecord.conversation_id == conversation_id)
    )
    if run_ids:
        source_plan_ids = select(ActionPlanRecord.id).where(
            ActionPlanRecord.task_run_id.in_(run_ids),
            ActionPlanRecord.user_id == user_id,
        )
        await session.scalars(
            select(ActionPlanRecord)
            .where(ActionPlanRecord.id.in_(source_plan_ids))
            .order_by(ActionPlanRecord.id)
            .with_for_update()
        )
        # Revoke pending actions and cooperatively stop active execution.
        # Already completed external actions keep their execution audit.
        await session.execute(
            update(ActionStepRecord)
            .where(
                ActionStepRecord.plan_id.in_(source_plan_ids),
                ActionStepRecord.status.in_({"ready", "awaiting_confirmation"}),
            )
            .values(
                status="cancelled", reason_code="source_deleted", completed_at=datetime.now(UTC)
            )
        )
        await session.execute(
            update(ActionPlanRecord)
            .where(
                ActionPlanRecord.id.in_(source_plan_ids),
                ActionPlanRecord.status.in_({"ready", "awaiting_confirmation"}),
            )
            .values(status="cancelled", cancelled_at=datetime.now(UTC))
        )
        await session.execute(
            delete(WorkflowDraftRecord).where(
                WorkflowDraftRecord.user_id == user_id,
                WorkflowDraftRecord.plan_id.in_(source_plan_ids),
            )
        )
        # Derived jobs must stop rather than becoming detached tasks.
        await session.execute(
            update(JobRecord)
            .where(
                JobRecord.task_run_id.in_(run_ids),
                JobRecord.owner == str(user_id),
                JobRecord.status.in_({"queued", "retry_wait", "waiting_user"}),
            )
            .values(
                status="cancelled",
                cancel_requested_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                lease_owner=None,
                lease_expires_at=None,
            )
        )
        await session.execute(
            update(JobRecord)
            .where(
                JobRecord.task_run_id.in_(run_ids),
                JobRecord.owner == str(user_id),
                JobRecord.status.in_({"admitted", "running"}),
            )
            .values(status="cancelling", cancel_requested_at=datetime.now(UTC))
        )
        await session.execute(
            delete(ModelReservationRecord).where(ModelReservationRecord.run_id.in_(run_ids))
        )
        await session.execute(
            delete(TaskRunEventRecord).where(TaskRunEventRecord.run_id.in_(run_ids))
        )
        await session.execute(
            update(JobRecord)
            .where(JobRecord.task_run_id.in_(run_ids))
            .values(task_run_id=None, input={"source_deleted": True})
        )
        await session.execute(
            update(ActionPlanRecord)
            .where(ActionPlanRecord.task_run_id.in_(run_ids))
            .values(
                task_run_id=None,
                reason_code="source_deleted",
                cancel_requested=True,
                cancel_reason="source_deleted",
            )
        )
    await session.execute(
        delete(TaskRunRecord).where(
            TaskRunRecord.conversation_id == conversation_id,
        )
    )
    await session.execute(
        delete(TimelineEventRecord).where(TimelineEventRecord.conversation_id == conversation_id)
    )
    await session.delete(conversation)
    return True
