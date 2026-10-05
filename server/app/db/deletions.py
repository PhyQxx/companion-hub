"""Shared source deletion cascade for live deletion and offline restore replay."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import String, cast, delete, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    ActionPlanRecord,
    ActionStepRecord,
    CognitiveGoalRecord,
    ConversationRecord,
    InteractionTurnRecord,
    JobRecord,
    JobStepRecord,
    MessageRecord,
    ModelReservationRecord,
    SkillDraftRecord,
    TaskRunEventRecord,
    TaskRunRecord,
    TimelineEventRecord,
    WorkflowDraftRecord,
)


async def _run_descendants(
    session: AsyncSession, conversation_id: UUID, user_id: UUID
) -> list[UUID]:
    """Owned transitive closure, parents first; visited IDs also bound cycles."""
    frontier = list(
        await session.scalars(
            select(TaskRunRecord.id)
            .where(
                TaskRunRecord.conversation_id == conversation_id,
                TaskRunRecord.user_id == user_id,
            )
            .order_by(TaskRunRecord.id)
        )
    )
    result: list[UUID] = []
    visited: set[UUID] = set()
    while frontier:
        current = [identifier for identifier in frontier if identifier not in visited]
        if not current:
            break
        result.extend(current)
        visited.update(current)
        frontier = list(
            await session.scalars(
                select(TaskRunRecord.id)
                .where(
                    TaskRunRecord.parent_run_id.in_(current),
                    TaskRunRecord.user_id == user_id,
                )
                .order_by(TaskRunRecord.id)
            )
        )
    # UUID order is not ancestry order (restored or supplied IDs may be older).
    # Admission locks ancestors before children, so do the same for all seeds.
    parents: dict[UUID, UUID | None] = {
        identifier: parent
        for identifier, parent in await session.execute(
            select(TaskRunRecord.id, TaskRunRecord.parent_run_id).where(
                TaskRunRecord.id.in_(result), TaskRunRecord.user_id == user_id
            )
        )
    }
    ordered: list[UUID] = []
    emitted: set[UUID] = set()
    for identifier in result:
        path: list[UUID] = []
        seen: set[UUID] = set()
        target: UUID | None = identifier
        while target in parents and target not in emitted and target not in seen:
            assert target is not None
            path.append(target)
            seen.add(target)
            target = parents[target]
        for node in reversed(path):
            ordered.append(node)
            emitted.add(node)
    return ordered


class _SourceGraphChanged(Exception):
    pass


async def _lock_source_graph(
    session: AsyncSession, conversation_id: UUID, user_id: UUID
) -> list[UUID]:
    # A child may commit while we wait for a parent. Never acquire its Job
    # after holding Run locks: a worker could hold that Job and await its Run.
    # Roll back this savepoint's locks and rediscover in the original order.
    for _ in range(8):
        try:
            async with session.begin_nested():
                run_ids = await _run_descendants(session, conversation_id, user_id)
                jobs = (
                    select(JobRecord.id)
                    .where(JobRecord.task_run_id.in_(run_ids))
                    .order_by(JobRecord.id)
                )
                plans = (
                    select(ActionPlanRecord.id)
                    .where(ActionPlanRecord.task_run_id.in_(run_ids))
                    .order_by(ActionPlanRecord.id)
                )
                job_ids = list(await session.scalars(jobs.with_for_update()))
                plan_ids = list(await session.scalars(plans.with_for_update()))
                for identifier in run_ids:
                    await session.scalar(
                        select(TaskRunRecord.id)
                        .where(
                            TaskRunRecord.id == identifier,
                            TaskRunRecord.user_id == user_id,
                        )
                        .with_for_update()
                    )
                if (
                    set(await _run_descendants(session, conversation_id, user_id)) != set(run_ids)
                    or list(await session.scalars(jobs)) != job_ids
                    or list(await session.scalars(plans)) != plan_ids
                ):
                    raise _SourceGraphChanged
            return run_ids
        except _SourceGraphChanged:
            continue
    # Fail closed and roll back the caller's transaction under sustained churn.
    raise RuntimeError("conversation_source_graph_changed")


async def purge_conversation(
    session: AsyncSession,
    conversation_id: UUID,
    *,
    user_id: UUID,
) -> bool:
    """Caller owns the transaction; never enqueue work or call an external provider."""
    conversation = await session.scalar(
        update(ConversationRecord)
        .where(ConversationRecord.id == conversation_id, ConversationRecord.user_id == user_id)
        .values(id=ConversationRecord.id)
        .returning(ConversationRecord)
        .execution_options(synchronize_session=False, populate_existing=True)
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
    run_ids = await _lock_source_graph(session, conversation_id, user_id)
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
        # The Job locks acquired above serialize all step lifecycle writes.
        # Keep content-free status/timing/progress audit, but not caller-defined
        # labels, checkpoints or error details after the source is forgotten.
        source_jobs = select(JobRecord.id).where(JobRecord.task_run_id.in_(run_ids))
        await session.execute(
            update(JobStepRecord)
            .where(JobStepRecord.job_id.in_(source_jobs))
            .values(
                name=literal("source_deleted:") + cast(JobStepRecord.id, String),
                checkpoint=None,
            )
        )
        await session.execute(
            update(JobRecord)
            .where(JobRecord.task_run_id.in_(run_ids))
            .values(
                task_run_id=None,
                input={"source_deleted": True},
                current_step=None,
                error_code=None,
                error_detail_safe=None,
            )
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
            TaskRunRecord.id.in_(run_ids),
            TaskRunRecord.user_id == user_id,
        )
    )
    await session.execute(
        delete(TimelineEventRecord).where(TimelineEventRecord.conversation_id == conversation_id)
    )
    await session.delete(conversation)
    return True
