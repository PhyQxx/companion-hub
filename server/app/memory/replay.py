"""删除台账重放：备份恢复后重新施加删除，防止已删内容复活。

按台账时间正序逐条重放：memory 实体按 ID 清链、message 实体连同
消息与其沉淀记忆一起清除。每一步都幂等——已删除的目标直接跳过，
重复重放计数归零。dry_run 只统计将要删除的对象，不产生任何写入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from uuid import UUID

from sqlalchemy import select

from app.db import ConversationRecord, Database, DeletionLedgerRecord, MessageRecord
from app.db.deletions import purge_conversation
from app.privacy.deletion_journal import DeletionJournal

from .store import MemoryStore

REPLAY_LEDGER_LIMIT = 10_000


@dataclass(frozen=True, slots=True)
class ReplayReport:
    ledger_rows: int
    conversations_deleted: int
    memories_deleted: int
    dry_run: bool


async def replay_deletions(database: Database, *, dry_run: bool = True) -> ReplayReport:
    """对当前数据库重放删除台账。

    备份恢复后，快照之前删除的行可能重新存活。按时间正序重放会把
    它们再次清除；每步幂等，已删除的目标直接跳过。
    """
    store = MemoryStore(database)
    conversations_deleted = 0
    memories_deleted = 0
    ledger_rows = 0
    last_id = 0
    # Keyset pagination scans the entire ledger, including the oldest entries.
    # A fixed newest-N limit silently leaves restored deleted data alive.
    async with database.sessions() as session:
        high_watermark = (
            await session.scalar(
                select(DeletionLedgerRecord.id).order_by(DeletionLedgerRecord.id.desc()).limit(1)
            )
            or 0
        )
    while last_id < high_watermark:
        async with database.sessions() as session:
            entries = list(
                await session.scalars(
                    select(DeletionLedgerRecord)
                    .where(
                        DeletionLedgerRecord.id > last_id, DeletionLedgerRecord.id <= high_watermark
                    )
                    .order_by(DeletionLedgerRecord.id)
                    .limit(REPLAY_LEDGER_LIMIT)
                )
            )
        if not entries:
            break
        for entry in entries:
            ledger_rows += 1
            if entry.entity_kind == "memory":
                memories_deleted += await _replay_memory_row(
                    store, tuple(entry.deleted_ids), dry_run
                )
            elif entry.entity_kind == "message":
                conversation_count, memory_count = await _replay_message_row(
                    database, store, entry.entity_id, dry_run, deleted_ids=tuple(entry.deleted_ids)
                )
                conversations_deleted += conversation_count
                memories_deleted += memory_count
        last_id = entries[-1].id
    return ReplayReport(
        ledger_rows=ledger_rows,
        conversations_deleted=conversations_deleted,
        memories_deleted=memories_deleted,
        dry_run=dry_run,
    )


async def _replay_memory_row(store: MemoryStore, memory_ids: tuple[int, ...], dry_run: bool) -> int:
    if not memory_ids:
        return 0
    if dry_run:
        return len(await store.existing_ids(memory_ids))
    return await store.purge_by_ids(memory_ids)


async def _replay_message_row(
    database: Database,
    store: MemoryStore,
    entity_id: str,
    dry_run: bool,
    *,
    deleted_ids: tuple[int, ...] = (),
) -> tuple[int, int]:
    try:
        conversation_id = UUID(entity_id)
    except ValueError:
        return 0, 0
    async with database.sessions() as session:
        conversation = await session.get(ConversationRecord, conversation_id)
    if conversation is None:
        return 0, await _replay_memory_row(store, deleted_ids, dry_run)
    async with database.sessions() as session:
        message_ids = [
            str(row)
            for row in await session.scalars(
                select(MessageRecord.id).where(MessageRecord.conversation_id == conversation_id)
            )
        ]
    linked = await store.memory_ids_by_source("message", message_ids)
    if dry_run:
        return 1, len(await store.existing_ids(linked))
    purged = await store.purge_by_source("message", message_ids)
    async with database.sessions.begin() as session:
        await purge_conversation(session, conversation_id, user_id=conversation.user_id)
    return 1, purged


async def import_deletion_journal(database: Database, journal: DeletionJournal) -> int:
    """Import validated intent before replay; the journal never supplies ledger PKs.

    All entries are parsed first, so malformed input cannot partially import.
    Files belong to the original installation: integer memory IDs cannot be
    interpreted against an unrelated database.
    """
    intents = await journal.read()
    inserted = 0
    async with database.sessions.begin() as session:
        for intent in intents:
            existing = list(
                await session.scalars(
                    select(DeletionLedgerRecord).where(
                        DeletionLedgerRecord.entity_kind == intent.entity_kind,
                        DeletionLedgerRecord.entity_id == intent.entity_id,
                    )
                )
            )
            if any(sorted(item.deleted_ids) == sorted(intent.deleted_ids) for item in existing):
                continue
            session.add(
                DeletionLedgerRecord(
                    entity_kind=intent.entity_kind,
                    entity_id=intent.entity_id,
                    deleted_ids=intent.deleted_ids,
                    requested_by="restore_journal",
                    reason="durable deletion intent replay",
                    created_at=intent.created_at.astimezone(UTC),
                )
            )
            await session.flush()
            inserted += 1
    return inserted
