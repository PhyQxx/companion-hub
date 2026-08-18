from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, select

from app.db import ConversationRecord, Database, InteractionTurnRecord, MessageRecord

from .store import MemoryStore

REPLAY_LEDGER_LIMIT = 10_000


@dataclass(frozen=True, slots=True)
class ReplayReport:
    ledger_rows: int
    conversations_deleted: int
    memories_deleted: int
    dry_run: bool


async def replay_deletions(database: Database, *, dry_run: bool = True) -> ReplayReport:
    """Re-applies the deletion ledger against the current database.

    After restoring a backup, rows deleted before the backup snapshot may be
    alive again. Replaying the ledger oldest-first removes them once more;
    every step is idempotent, so already-deleted targets are simply skipped.
    """
    store = MemoryStore(database)
    entries = await store.list_deletion_ledger(limit=REPLAY_LEDGER_LIMIT)
    conversations_deleted = 0
    memories_deleted = 0
    for entry in reversed(entries):
        if entry.entity_kind == "memory":
            memories_deleted += await _replay_memory_row(store, entry.deleted_ids, dry_run)
        elif entry.entity_kind == "message":
            conversation_count, memory_count = await _replay_message_row(
                database, store, entry.entity_id, dry_run
            )
            conversations_deleted += conversation_count
            memories_deleted += memory_count
    return ReplayReport(
        ledger_rows=len(entries),
        conversations_deleted=conversations_deleted,
        memories_deleted=memories_deleted,
        dry_run=dry_run,
    )


async def _replay_memory_row(
    store: MemoryStore, memory_ids: tuple[int, ...], dry_run: bool
) -> int:
    if not memory_ids:
        return 0
    if dry_run:
        return len(await store.existing_ids(memory_ids))
    return await store.purge_by_ids(memory_ids)


async def _replay_message_row(
    database: Database, store: MemoryStore, entity_id: str, dry_run: bool
) -> tuple[int, int]:
    try:
        conversation_id = UUID(entity_id)
    except ValueError:
        return 0, 0
    async with database.sessions() as session:
        conversation = await session.get(ConversationRecord, conversation_id)
    if conversation is None:
        return 0, 0
    async with database.sessions() as session:
        message_ids = [
            str(row)
            for row in await session.scalars(
                select(MessageRecord.id).where(
                    MessageRecord.conversation_id == conversation_id
                )
            )
        ]
    linked = await store.memory_ids_by_source("message", message_ids)
    if dry_run:
        return 1, len(await store.existing_ids(linked))
    purged = await store.purge_by_source("message", message_ids)
    async with database.sessions.begin() as session:
        await session.execute(
            delete(InteractionTurnRecord).where(
                InteractionTurnRecord.conversation_id == conversation_id
            )
        )
        await session.execute(
            delete(MessageRecord).where(MessageRecord.conversation_id == conversation_id)
        )
        await session.execute(
            delete(ConversationRecord).where(ConversationRecord.id == conversation_id)
        )
    return 1, purged
