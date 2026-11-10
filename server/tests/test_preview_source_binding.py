"""B-01 确认预览来源绑定：删源/撤销作废未确认预览，在途与回执如实保留。"""

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import update
from test_domain_delete_fence import storage_mode
from test_resource_budget import seed

from app.confirmation import DatabasePendingMutationStore, PendingMutation
from app.db import (
    ConversationRecord,
    Database,
    InteractionTurnRecord,
    MessageRecord,
    TaskRunRecord,
)
from app.db.deletions import purge_conversation
from app.ids import uuid7
from app.runs.store import RunStore


async def _seed_sourced_turn(
    database: Database,
    owner: UUID,
    run_id: UUID,
    conversation: UUID,
    *,
    state: str,
    turn_seq: int,
    message_seq: int,
) -> UUID:
    turn_id = uuid7()
    message_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(
            MessageRecord(
                id=message_id,
                conversation_id=conversation,
                turn_id=turn_id,
                seq=message_seq,
                role="user",
                content="hi",
                privacy_level="L1",
            )
        )
        session.add(
            InteractionTurnRecord(
                id=turn_id,
                conversation_id=conversation,
                task_run_id=run_id,
                turn_seq=turn_seq,
                generation_id=uuid7(),
                state=state,
                state_version=1,
                input_message_id=message_id,
            )
        )
    return turn_id


async def _preview(
    store: DatabasePendingMutationStore,
    owner: UUID,
    turn_id: UUID,
    subject: str,
    *,
    kind: str,
) -> PendingMutation:
    return await store.prepare(
        user_id=owner,
        turn_id=turn_id,
        kind=kind,
        content={"subject": subject},
        preview={"subject": subject},
    )


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
async def test_conversation_purge_revokes_pending_previews_and_keeps_receipts(
    backend: str, tmp_path: Path
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        database = storage.database
        owner, run_id, _ = await seed(database)
        conversation = uuid7()
        async with database.sessions.begin() as session:
            session.add(ConversationRecord(id=conversation, user_id=owner))
            await session.flush()
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == run_id)
                .values(conversation_id=conversation)
            )
        turn_id = await _seed_sourced_turn(
            database, owner, run_id, conversation, state="completed", turn_seq=1, message_seq=1
        )
        store = DatabasePendingMutationStore(database)
        pending = await _preview(store, owner, turn_id, "待确认", kind="mail_send")
        in_flight = await _preview(store, owner, turn_id, "在途", kind="calendar_create")
        await store.claim(owner, in_flight.id, in_flight.digest)
        sent = await _preview(store, owner, turn_id, "已发出", kind="workflow_save")
        claimed = await store.claim(owner, sent.id, sent.digest)
        await store.complete(claimed, {"receipt": "sent"})

        async with database.sessions.begin() as session:
            assert await purge_conversation(session, conversation, user_id=owner)

        # 未确认预览随来源删除：不可再受理
        with pytest.raises(LookupError, match="not_found"):
            await store.claim(owner, pending.id, pending.digest)
        # 在途认领保留，外部结果仍可如实落回执
        late = await store.complete(in_flight, {"receipt": "late"})
        assert late.status == "completed"
        remaining = await store.list(owner)
        remaining_ids = {item.id for item in remaining}
        assert pending.id not in remaining_ids
        statuses = {item.id: item.status for item in remaining}
        assert statuses[in_flight.id] == "completed"
        sent_row = next(item for item in remaining if item.id == sent.id)
        assert sent_row.status == "completed" and sent_row.result == {"receipt": "sent"}


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
async def test_run_cancel_revokes_pending_previews_across_turn_states(
    backend: str, tmp_path: Path
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        database = storage.database
        owner, run_id, _ = await seed(database)
        conversation = uuid7()
        async with database.sessions.begin() as session:
            session.add(ConversationRecord(id=conversation, user_id=owner, title="Fixture"))
            await session.flush()
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == run_id)
                .values(conversation_id=conversation)
            )
        active_turn = await _seed_sourced_turn(
            database, owner, run_id, conversation, state="thinking", turn_seq=1, message_seq=1
        )
        done_turn = await _seed_sourced_turn(
            database, owner, run_id, conversation, state="completed", turn_seq=2, message_seq=2
        )
        store = DatabasePendingMutationStore(database)
        pending_active = await _preview(store, owner, active_turn, "活动中待确认", kind="mail_send")
        pending_done = await _preview(store, owner, done_turn, "已结束待确认", kind="mail_send")
        in_flight = await _preview(store, owner, active_turn, "在途", kind="calendar_create")
        await store.claim(owner, in_flight.id, in_flight.digest)

        changed, _ = await RunStore(database).cancel_work(run_id, user_id=owner)
        assert changed

        # 撤销后未确认预览不得再受理（含已完成回合的残留预览）
        with pytest.raises(ValueError, match="not_pending"):
            await store.claim(owner, pending_active.id, pending_active.digest)
        with pytest.raises(ValueError, match="not_pending"):
            await store.claim(owner, pending_done.id, pending_done.digest)
        # 在途认领不受撤销影响，回执如实落库
        completed = await store.complete(in_flight, {"receipt": "sent"})
        assert completed.status == "completed"
        statuses = {item.id: item.status for item in await store.list(owner)}
        assert statuses[pending_active.id] == "cancelled"
        assert statuses[pending_done.id] == "cancelled"
        assert statuses[in_flight.id] == "completed"
