"""Enroll source work atomically while its owned run is still live."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import ConversationRecord, JobRecord, TaskRunRecord
from app.db.claims import assert_current_claim

from .store import append_run_event


async def lock_source_run(session: AsyncSession, run_id: UUID, *, user_id: UUID) -> TaskRunRecord:
    # Locate and fence the conversation in the first write, preserving the
    # conversation -> Job -> Run order without a SQLite read-lock upgrade.
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
    # Background roots have a Job; chat roots normally do not. Do not read
    # private Job payloads or lock another owner's root while locating it.
    await session.execute(
        update(JobRecord)
        .where(JobRecord.id == run_id, JobRecord.owner == str(user_id))
        .values(progress=JobRecord.progress)
        .execution_options(synchronize_session=False)
    )
    await assert_current_claim(session)
    row = await session.scalar(
        update(TaskRunRecord)
        .where(TaskRunRecord.id == run_id, TaskRunRecord.user_id == user_id)
        .values(updated_at=TaskRunRecord.updated_at)
        .returning(TaskRunRecord)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    if row is None:
        owner = await session.scalar(
            select(TaskRunRecord.user_id).where(TaskRunRecord.id == run_id)
        )
        if owner is not None and owner != user_id:
            raise PermissionError("task_run_owner_mismatch")
        raise LookupError("task run not found")
    if row.conversation_id != conversation:
        raise LookupError("task source deleted")
    if row.contract.get("criterion") == "model_result_returned":
        raise ValueError("model_only_run_cannot_enroll_work")
    if row.contract.get("criterion") == "delivery_channels_returned":
        raise ValueError("delivery_only_run_cannot_enroll_work")
    if row.contract.get("criterion") == "provider_response_returned":
        raise ValueError("provider_only_run_cannot_enroll_work")
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
