"""Enroll source work atomically while its owned run is still live."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import ConversationRecord, JobRecord, TaskRunRecord
from app.db.claims import assert_current_claim

from .store import append_run_event


async def lock_source_run(session: AsyncSession, run_id: UUID, *, user_id: UUID) -> TaskRunRecord:
    row = await session.get(TaskRunRecord, run_id)
    if row is None:
        raise LookupError("task run not found")
    if row.user_id != user_id:
        raise PermissionError("task_run_owner_mismatch")
    if row.contract.get("criterion") == "model_result_returned":
        raise ValueError("model_only_run_cannot_enroll_work")
    if row.conversation_id is not None:
        conversation = await session.scalar(
            select(ConversationRecord)
            .where(
                ConversationRecord.id == row.conversation_id,
                ConversationRecord.user_id == user_id,
            )
            .with_for_update()
        )
        if conversation is None:
            raise LookupError("task source deleted")
    else:
        # Independent background runs use their root job as the enrollment
        # fence, matching cancellation and model admission lock ordering.
        await session.scalar(select(JobRecord).where(JobRecord.id == run_id).with_for_update())
    await assert_current_claim(session)
    row = await session.get(TaskRunRecord, run_id, with_for_update=True, populate_existing=True)
    if row is None or row.user_id != user_id:
        raise LookupError("task run not found")
    if row.status not in {"accepted", "running", "succeeded"} or row.contract.get(
        "work_cancel_requested"
    ):
        raise ValueError("task_run_inactive")
    return row


async def require_work(
    session: AsyncSession, row: TaskRunRecord, *, kind: str, work_id: UUID
) -> None:
    work = list(row.contract.get("required_work", []))
    if any(item.get("kind") == kind and item.get("id") == str(work_id) for item in work):
        return
    work.append({"kind": kind, "id": str(work_id)})
    row.contract = {**row.contract, "required_work": work}
    row.state_version += 1
    row.updated_at = datetime.now(UTC)
    await append_run_event(
        session, row, "run.work.required", payload={"kind": kind, "work_id": str(work_id)}
    )
