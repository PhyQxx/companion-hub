"""Deleting a source also revokes its owned descendants and retains accounting."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_domain_delete_fence import storage_mode
from test_job_lifecycle_fence import during_commit
from test_resource_budget import seed

from app.db import (
    ActionPlanRecord,
    ConversationRecord,
    JobRecord,
    ModelCostRecord,
    ModelReservationRecord,
    TaskRunEventRecord,
    TaskRunRecord,
    WorkflowDraftRecord,
)
from app.db.deletions import purge_conversation
from app.ids import uuid7


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
@pytest.mark.parametrize("state", ["queued", "running", "succeeded"])
async def test_purge_removes_owned_descendants_and_revokes_work(
    backend: str, state: str, tmp_path: Path
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        db = storage.database
        owner, root, _ = await seed(db)
        other, unrelated, _ = await seed(db)
        conversation, child, grandchild, independent = (uuid7() for _ in range(4))
        plan, draft, job, event, cost = (uuid7() for _ in range(5))
        now = datetime.now(UTC)
        async with db.sessions.begin() as sql:
            sql.add(ConversationRecord(id=conversation, user_id=owner, title="synthetic"))
            await sql.flush()
            await sql.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == root)
                .values(conversation_id=conversation)
            )
            for identifier, parent in ((child, root), (grandchild, child)):
                sql.add(
                    TaskRunRecord(
                        id=identifier,
                        user_id=owner,
                        parent_run_id=parent,
                        status="running",
                        privacy_level="L1",
                        contract={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                await sql.flush()
            sql.add(
                TaskRunRecord(
                    id=independent,
                    user_id=owner,
                    status="running",
                    privacy_level="L1",
                    contract={},
                    created_at=now,
                    updated_at=now,
                )
            )
            sql.add(
                JobRecord(
                    id=job,
                    owner=str(owner),
                    task_run_id=grandchild,
                    kind="deleg.fixture",
                    status=state,
                    input={"private": "synthetic"},
                )
            )
            sql.add(
                ActionPlanRecord(
                    id=plan,
                    user_id=owner,
                    task_run_id=grandchild,
                    status="completed" if state == "succeeded" else "ready",
                    idempotency_key="descendant",
                    request_hash="fixture",
                    expires_at=now + timedelta(minutes=5),
                )
            )
            sql.add(
                WorkflowDraftRecord(
                    id=draft,
                    user_id=owner,
                    plan_id=plan,
                    name="synthetic",
                    steps=[],
                    dedupe_key="descendant",
                )
            )
            sql.add(
                TaskRunEventRecord(
                    event_id=event,
                    run_id=grandchild,
                    seq=1,
                    kind="fixture",
                    privacy_level="L1",
                    occurred_at=now,
                    payload={"private": "synthetic"},
                )
            )
            sql.add(
                ModelCostRecord(
                    call_id=cost,
                    user_id=owner,
                    endpoint="fixture",
                    state="unknown",
                    created_at=now,
                )
            )
            sql.add(
                ModelReservationRecord(
                    call_id=cost,
                    run_id=grandchild,
                    phase="maintenance",
                    endpoint="fixture",
                    state="unknown",
                    reserved_tokens=1,
                    charged_tokens=1,
                    created_at=now,
                )
            )
        async with db.sessions.begin() as sql:
            assert not await purge_conversation(sql, conversation, user_id=other)
        async with db.sessions.begin() as sql:
            assert await purge_conversation(sql, conversation, user_id=owner)
        async with db.sessions() as sql:
            for identifier in (root, child, grandchild):
                assert await sql.get(TaskRunRecord, identifier) is None
            assert await sql.get(TaskRunRecord, unrelated) is not None
            assert await sql.get(TaskRunRecord, independent) is not None
            assert await sql.get(WorkflowDraftRecord, draft) is None
            assert await sql.get(TaskRunEventRecord, event) is None
            assert await sql.get(ModelCostRecord, cost) is not None
            assert await sql.get(ModelReservationRecord, cost) is None
            saved_job = await sql.get_one(JobRecord, job)
            assert (
                saved_job.status
                == {"queued": "cancelled", "running": "cancelling", "succeeded": "succeeded"}[state]
            )
            assert saved_job.input == {"source_deleted": True}
            assert saved_job.task_run_id is None
            saved_plan = await sql.get_one(ActionPlanRecord, plan)
            assert saved_plan.status == ("completed" if state == "succeeded" else "cancelled")
            assert saved_plan.cancel_requested and saved_plan.task_run_id is None
        async with db.sessions.begin() as sql:
            assert not await purge_conversation(sql, conversation, user_id=owner)


async def test_purge_rediscovers_child_committed_while_waiting_for_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with storage_mode("postgresql", tmp_path) as storage:
        db = storage.database
        owner, root, _ = await seed(db)
        conversation, child, job = uuid7(), uuid7(), uuid7()
        now = datetime.now(UTC)
        async with db.sessions.begin() as sql:
            sql.add(ConversationRecord(id=conversation, user_id=owner))
            await sql.flush()
            await sql.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == root)
                .values(conversation_id=conversation)
            )

        async def admit(sql: object) -> None:
            # Use the real parent write fence held by child admission.
            assert isinstance(sql, AsyncSession)
            await sql.execute(
                update(TaskRunRecord).where(TaskRunRecord.id == root).values(updated_at=now)
            )
            sql.add(
                TaskRunRecord(
                    id=child,
                    user_id=owner,
                    parent_run_id=root,
                    status="running",
                    privacy_level="L1",
                    contract={},
                    created_at=now,
                    updated_at=now,
                )
            )
            await sql.flush()
            sql.add(
                JobRecord(
                    id=job,
                    owner=str(owner),
                    task_run_id=child,
                    kind="deleg.fixture",
                    status="queued",
                    input={"private": "synthetic"},
                )
            )
            await sql.flush()

        async def purge() -> None:
            async with db.sessions.begin() as sql:
                assert await purge_conversation(sql, conversation, user_id=owner)

        from app.db import deletions

        original = deletions._run_descendants
        snapshots: list[list[UUID]] = []

        async def observe(sql: AsyncSession, source: UUID, user: UUID) -> list[UUID]:
            # Keep the real database implementation and capture each discovery.
            result = await original(sql, source, user)
            snapshots.append(list(result))
            return result

        monkeypatch.setattr(deletions, "_run_descendants", observe)
        await during_commit(db, admit, purge)
        assert child not in snapshots[0]
        assert any(child in snapshot for snapshot in snapshots[1:])
        async with db.sessions() as sql:
            assert list(await sql.scalars(select(TaskRunRecord.id))) == []
            saved_job = await sql.get_one(JobRecord, job)
            assert saved_job.status == "cancelled"
            assert saved_job.input == {"source_deleted": True}


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
@pytest.mark.parametrize("change", ["owner", "deleted", "unchanged"])
async def test_conversation_delete_rechecks_owner_after_writer_commit(
    backend: str, change: str, tmp_path: Path
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        db = storage.database
        owner, root, _ = await seed(db)
        other, _, _ = await seed(db)
        conversation = uuid7()
        async with db.sessions.begin() as sql:
            sql.add(ConversationRecord(id=conversation, user_id=owner))
            await sql.flush()
            await sql.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == root)
                .values(conversation_id=conversation)
            )

        async def purge() -> None:
            async with db.sessions.begin() as sql:
                result = await purge_conversation(sql, conversation, user_id=owner)
                assert result is (change == "unchanged")

        await during_commit(
            db,
            lambda sql: sql.execute(
                delete(ConversationRecord).where(ConversationRecord.id == conversation)
                if change == "deleted"
                else update(ConversationRecord)
                .where(ConversationRecord.id == conversation)
                .values(user_id=other if change == "owner" else owner)
            ),
            purge,
        )
        async with db.sessions() as sql:
            saved = await sql.get(ConversationRecord, conversation)
            if change == "owner":
                assert saved is not None and saved.user_id == other
                assert await sql.get(TaskRunRecord, root) is not None
            else:
                assert saved is None


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
async def test_legacy_parent_cycle_is_finite_and_deleted(backend: str, tmp_path: Path) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        db = storage.database
        owner, root, _ = await seed(db)
        conversation, child = uuid7(), uuid7()
        now = datetime.now(UTC)
        async with db.sessions.begin() as sql:
            sql.add(ConversationRecord(id=conversation, user_id=owner))
            await sql.flush()
            sql.add(
                TaskRunRecord(
                    id=child,
                    user_id=owner,
                    parent_run_id=root,
                    status="running",
                    privacy_level="L1",
                    contract={},
                    created_at=now,
                    updated_at=now,
                )
            )
            await sql.flush()
            await sql.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == root)
                .values(conversation_id=conversation, parent_run_id=child)
            )
        async with db.sessions.begin() as sql:
            assert await purge_conversation(sql, conversation, user_id=owner)
        async with db.sessions() as sql:
            assert await sql.get(TaskRunRecord, root) is None
            assert await sql.get(TaskRunRecord, child) is None
