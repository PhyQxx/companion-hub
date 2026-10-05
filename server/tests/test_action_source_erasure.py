"""Erase never-started source parameters without discarding execution audits."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from test_domain_delete_fence import storage_mode
from test_job_lifecycle_fence import during_commit
from test_resource_budget import seed
from test_workflow_drafts import _registry

from app.cognition import ActionInvocation, ActionPlanService
from app.db import ActionPlanRecord, ActionStepRecord, ConversationRecord, TaskRunRecord
from app.db.deletions import purge_conversation
from app.ids import uuid7


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
@pytest.mark.parametrize(
    "state",
    [
        "ready",
        "awaiting_confirmation",
        "cancelled",
        "skipped",
        "expired",
        "executing",
        "completed",
        "failed",
        "unknown_outcome",
    ],
)
async def test_source_cleanup_distinguishes_unstarted_requests_and_attempt_audits(
    backend: str, state: str, tmp_path: Path
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        db = storage.database
        owner, root, _ = await seed(db)
        conversation = uuid7()
        now = datetime.now(UTC)
        async with db.sessions.begin() as sql:
            sql.add(ConversationRecord(id=conversation, user_id=owner))
            await sql.flush()
            await sql.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == root)
                .values(conversation_id=conversation)
            )
        service = ActionPlanService(db, _registry())
        invocation = ActionInvocation(
            action_id="test.read_state", arguments={"target": "synthetic secret"}
        )
        plan = await service.create_plan(
            user_id=owner,
            title="synthetic secret title",
            invocations=[invocation],
            source_turn_id=root,
        )
        independent = await service.create_plan(
            user_id=owner,
            title="independent title",
            invocations=[invocation],
        )
        step = plan.steps[0]
        attempted = state in {"executing", "completed", "failed", "unknown_outcome"}
        result = {"private": "synthetic secret result"} if attempted else None
        async with db.sessions.begin() as sql:
            await sql.execute(
                update(ActionStepRecord)
                .where(ActionStepRecord.id == step.id)
                .values(
                    status=state,
                    started_at=now if attempted else None,
                    result=result,
                    verification_result={"private": "synthetic secret verification"},
                )
            )
            await sql.execute(
                update(ActionPlanRecord)
                .where(ActionPlanRecord.id == plan.id)
                .values(status="executing" if attempted else "ready")
            )
        async with db.sessions.begin() as sql:
            assert await purge_conversation(sql, conversation, user_id=owner)
        async with db.sessions() as sql:
            saved_plan = await sql.get_one(ActionPlanRecord, plan.id)
            saved_step = await sql.get_one(ActionStepRecord, step.id)
            assert saved_plan.cancel_requested and saved_plan.reason_code == "source_deleted"
            assert saved_plan.task_run_id is None
            assert (
                saved_step.action_id == step.action_id
                and saved_step.idempotency_key == step.idempotency_key
            )
            if attempted:
                assert saved_plan.title == "synthetic secret title"
                assert (
                    saved_step.arguments == step.arguments
                    and saved_step.tool_arguments == step.tool_arguments
                )
                assert saved_step.result == result and saved_step.started_at is not None
                assert saved_step.verification_result == {
                    "private": "synthetic secret verification"
                }
                assert saved_step.status == state
            else:
                assert saved_plan.title is None
                assert saved_step.arguments == {} and saved_step.tool_arguments == {}
                assert saved_step.verification_result is None
                assert saved_step.status == (
                    "cancelled" if state in {"ready", "awaiting_confirmation"} else state
                )
            retained = await sql.get_one(ActionStepRecord, independent.steps[0].id)
            assert retained.arguments == independent.steps[0].arguments
            assert (
                await sql.get_one(ActionPlanRecord, independent.id)
            ).title == "independent title"
        # Cancellation prevents a cleared request from being claimed or executed.
        if attempted:
            assert await service._claim_next_step(user_id=owner, plan_id=plan.id) is None
        else:
            with pytest.raises(ValueError, match="not executing"):
                await service._claim_next_step(user_id=owner, plan_id=plan.id)


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
async def test_waiting_claim_cannot_execute_a_request_erased_with_its_source(
    backend: str, tmp_path: Path
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        db = storage.database
        owner, root, _ = await seed(db)
        conversation = uuid7()
        async with db.sessions.begin() as sql:
            sql.add(ConversationRecord(id=conversation, user_id=owner))
            await sql.flush()
            await sql.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == root)
                .values(conversation_id=conversation)
            )
        service = ActionPlanService(db, _registry())
        plan = await service.create_plan(
            user_id=owner,
            invocations=[
                ActionInvocation(
                    action_id="test.read_state", arguments={"target": "synthetic secret"}
                )
            ],
            source_turn_id=root,
        )
        async with db.sessions.begin() as sql:
            await sql.execute(
                update(ActionPlanRecord)
                .where(ActionPlanRecord.id == plan.id)
                .values(status="executing")
            )

        async def erase(sql: AsyncSession) -> None:
            assert await purge_conversation(sql, conversation, user_id=owner)

        async def claim() -> None:
            assert await service._claim_next_step(user_id=owner, plan_id=plan.id) is None

        await during_commit(db, erase, claim)
        async with db.sessions() as sql:
            step = await sql.get_one(ActionStepRecord, plan.steps[0].id)
            assert step.status == "cancelled" and step.started_at is None
            assert step.arguments == {} and step.tool_arguments == {}
