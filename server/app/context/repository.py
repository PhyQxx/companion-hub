"""Translate authorized domain snapshots to content-free harness references."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    ConversationRecord,
    DeletionLedgerRecord,
    MemoryRecord,
    MemorySourceRecord,
    MessageRecord,
    SkillRecord,
    TimelineEventRecord,
)
from app.harness.context import ContextReference
from app.memory import MemoryHit
from app.timeline.models import TimelineEvent


def version_stamp(value: datetime) -> str:
    return (
        value.replace(tzinfo=UTC).isoformat()
        if value.tzinfo is None
        else value.astimezone(UTC).isoformat()
    )


def memory_reference(hit: MemoryHit, *, included: bool) -> ContextReference:
    memory = hit.memory
    return ContextReference(
        kind="memory",
        source_id=str(memory.id),
        owner_id=str(memory.user_id),
        privacy_level=memory.privacy_level,
        version=version_stamp(memory.updated_at),
        included=included,
        reason="grounded" if included else "not_grounded",
    )


def timeline_reference(event: TimelineEvent) -> ContextReference:
    return ContextReference(
        kind="timeline",
        source_id=str(event.id),
        owner_id=str(event.user_id),
        privacy_level=event.privacy_level,
        version=version_stamp(event.created_at),
        parent_kind=event.source_type,
        parent_id=event.source_id,
        lineage=(("conversation", str(event.conversation_id)),) if event.conversation_id else (),
    )


async def attach_memory_lineage(
    session: AsyncSession,
    references: tuple[ContextReference, ...],
    *,
    owner_id: UUID,
) -> tuple[ContextReference, ...]:
    ids = [int(ref.source_id) for ref in references]
    if not ids:
        return references
    rows = list(
        await session.scalars(
            select(MemorySourceRecord)
            .join(
                MemoryRecord,
                MemoryRecord.id == MemorySourceRecord.memory_id,
            )
            .where(MemoryRecord.user_id == owner_id, MemorySourceRecord.memory_id.in_(ids))
            .limit(1001)
        )
    )
    if len(rows) > 1000:
        raise ContextSourceInvalidated()
    lineage: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        lineage.setdefault(str(row.memory_id), []).append((row.source_kind, row.source_id))
    return tuple(
        replace(ref, lineage=tuple(sorted(lineage.get(ref.source_id, [])))) for ref in references
    )


class ContextSourceInvalidated(ValueError):
    def __init__(self) -> None:
        super().__init__("context_source_invalidated")


async def validate_references(
    session: AsyncSession,
    references: tuple[ContextReference, ...],
    *,
    owner_id: UUID,
    privacy_level: str,
    lock: bool = False,
) -> None:
    """Batch-check live owner/privacy/version; never accept deleted snapshots."""
    if any(
        ref.included and (ref.owner_id != str(owner_id) or ref.privacy_level > privacy_level)
        for ref in references
    ):
        raise ContextSourceInvalidated()
    await _validate_lineage(session, references, owner_id=owner_id, privacy_level=privacy_level)
    skill_refs = [ref for ref in references if ref.kind == "skill" and ref.included]
    if skill_refs:
        skill_query = select(SkillRecord).where(
            SkillRecord.id.in_([UUID(ref.source_id) for ref in skill_refs])
        )
        if lock:
            skill_query = skill_query.with_for_update()
        skills = {str(row.id): row for row in await session.scalars(skill_query)}
        for ref in skill_refs:
            skill = skills.get(ref.source_id)
            if (
                skill is None
                or not skill.enabled
                or str(skill.version) != ref.version
                or ref.owner_id != str(owner_id)
                or ref.privacy_level > privacy_level
            ):
                raise ContextSourceInvalidated()
    for ref in references:
        if ref.kind != "summary" or not ref.included:
            continue
        conversation = await session.get(ConversationRecord, UUID(ref.source_id))
        if (
            conversation is None
            or conversation.user_id != owner_id
            or ref.owner_id != str(owner_id)
        ):
            raise ContextSourceInvalidated()
        count, maximum = (
            await session.execute(
                select(
                    func.count(MessageRecord.id),
                    func.max(MessageRecord.privacy_level),
                ).where(
                    MessageRecord.conversation_id == conversation.id,
                    MessageRecord.seq <= int(ref.version or "0"),
                )
            )
        ).one()
        if count != ref.source_count or (maximum and maximum > privacy_level):
            raise ContextSourceInvalidated()
    models: tuple[
        tuple[str, type[MemoryRecord] | type[TimelineEventRecord] | type[MessageRecord]], ...
    ] = (
        ("memory", MemoryRecord),
        ("timeline", TimelineEventRecord),
        ("message", MessageRecord),
    )
    for kind, model in models:
        selected = [ref for ref in references if ref.included and ref.kind == kind]
        if not selected:
            continue
        ids = [UUID(ref.source_id) if kind == "message" else int(ref.source_id) for ref in selected]
        query = select(model).where(model.id.in_(ids))
        if model is MessageRecord:
            query = query.join(
                ConversationRecord,
                ConversationRecord.id == MessageRecord.conversation_id,
            ).where(
                ConversationRecord.user_id == owner_id,
            )
        if lock:
            query = query.with_for_update()
        rows = cast(
            Iterable[MemoryRecord | TimelineEventRecord | MessageRecord],
            await session.scalars(query),
        )
        records = {str(row.id): row for row in rows}
        for ref in selected:
            row = records.get(ref.source_id)
            if row is None or ref.owner_id != str(owner_id) or row.privacy_level > privacy_level:
                raise ContextSourceInvalidated()
            if isinstance(row, MessageRecord):
                # Conversation ownership has already been locked by ChatService.
                if ref.version != str(row.seq):
                    raise ContextSourceInvalidated()
            else:
                if row.user_id != owner_id:
                    raise ContextSourceInvalidated()
                stamp = row.updated_at if isinstance(row, MemoryRecord) else row.created_at
                if ref.version != version_stamp(stamp):
                    raise ContextSourceInvalidated()
                if isinstance(row, TimelineEventRecord) and ref.parent_kind is not None:
                    if ref.parent_kind != row.source_type or ref.parent_id != row.source_id:
                        raise ContextSourceInvalidated()
                    known_conversations = {
                        identifier for kind, identifier in ref.lineage if kind == "conversation"
                    }
                    if known_conversations and known_conversations != {str(row.conversation_id)}:
                        raise ContextSourceInvalidated()
                if isinstance(row, MemoryRecord) and row.status != "active":
                    raise ContextSourceInvalidated()


async def _validate_lineage(
    session: AsyncSession,
    references: tuple[ContextReference, ...],
    *,
    owner_id: UUID,
    privacy_level: str,
) -> None:
    """Bounded owned message/memory ancestry and deletion intents, without bodies."""
    memory_ids: set[int] = set()
    message_ids: set[UUID] = set()
    optional_message_ids: set[UUID] = set()
    optional_conversation_ids: set[UUID] = set()
    conversation_ids: set[UUID] = set()
    expected_lineage: dict[int, set[tuple[str, str]]] = {}
    try:
        for ref in references:
            if not ref.included:
                continue
            if ref.kind == "memory":
                memory_ids.add(int(ref.source_id))
                if ref.lineage:
                    expected_lineage[int(ref.source_id)] = set(ref.lineage)
            elif ref.kind == "message":
                message_ids.add(UUID(ref.source_id))
            elif ref.kind == "summary":
                conversation_ids.add(UUID(ref.source_id))
            if ref.kind == "timeline":
                if ref.parent_kind == "message" and ref.parent_id:
                    optional_message_ids.add(UUID(ref.parent_id))
                for kind, identifier in ref.lineage:
                    if kind == "conversation":
                        optional_conversation_ids.add(UUID(identifier))
        pending = set(memory_ids)
        visited: set[int] = set()
        for _ in range(16):
            if not pending:
                break
            if len(visited | pending) > 1000 or any(
                value < 1 or value > 2**63 - 1 for value in pending
            ):
                raise ContextSourceInvalidated()
            live = set(
                await session.scalars(
                    select(MemoryRecord.id).where(
                        MemoryRecord.id.in_(pending),
                        MemoryRecord.user_id == owner_id,
                        MemoryRecord.privacy_level <= privacy_level,
                    )
                )
            )
            if live != pending:
                raise ContextSourceInvalidated()
            parents = list(
                await session.scalars(
                    select(MemorySourceRecord)
                    .where(
                        MemorySourceRecord.memory_id.in_(pending),
                    )
                    .limit(1001)
                )
            )
            if len(parents) > 1000:
                raise ContextSourceInvalidated()
            current_lineage: dict[int, set[tuple[str, str]]] = {value: set() for value in pending}
            for parent in parents:
                current_lineage[parent.memory_id].add((parent.source_kind, parent.source_id))
            if any(
                current_lineage[value] != expected_lineage[value]
                for value in pending
                if value in expected_lineage
            ):
                raise ContextSourceInvalidated()
            visited.update(pending)
            pending = set()
            for parent in parents:
                if parent.source_kind == "message":
                    message_ids.add(UUID(parent.source_id))
                elif parent.source_kind == "memory":
                    pending.add(int(parent.source_id))
            pending -= visited
        if (
            pending
            or len(message_ids | optional_message_ids)
            + len(conversation_ids | optional_conversation_ids)
            > 1000
        ):
            raise ContextSourceInvalidated()
        memory_ids = visited
    except (ValueError, TypeError, OverflowError) as error:
        if isinstance(error, ContextSourceInvalidated):
            raise
        raise ContextSourceInvalidated() from error
    if optional_conversation_ids:
        owners = await session.scalars(
            select(ConversationRecord.user_id).where(
                ConversationRecord.id.in_(optional_conversation_ids)
            )
        )
        if any(value != owner_id for value in owners):
            raise ContextSourceInvalidated()
        conversation_ids.update(optional_conversation_ids)
    all_message_ids = message_ids | optional_message_ids
    if all_message_ids:
        rows = list(
            await session.execute(
                select(
                    MessageRecord.id,
                    MessageRecord.conversation_id,
                    MessageRecord.privacy_level,
                    ConversationRecord.user_id,
                )
                .outerjoin(
                    ConversationRecord, ConversationRecord.id == MessageRecord.conversation_id
                )
                .where(MessageRecord.id.in_(all_message_ids))
            )
        )
        if not message_ids.issubset({row[0] for row in rows}):
            raise ContextSourceInvalidated()
        if any(row[3] != owner_id or row[2] > privacy_level for row in rows):
            raise ContextSourceInvalidated()
        conversation_ids.update(row[1] for row in rows)
    if memory_ids or all_message_ids or conversation_ids:
        deleted = await session.scalar(
            select(DeletionLedgerRecord.id)
            .where(
                or_(
                    and_(
                        DeletionLedgerRecord.entity_kind == "memory",
                        DeletionLedgerRecord.entity_id.in_([str(value) for value in memory_ids]),
                    ),
                    and_(
                        DeletionLedgerRecord.entity_kind == "message",
                        func.lower(func.replace(DeletionLedgerRecord.entity_id, "-", "")).in_(
                            [value.hex for value in all_message_ids | conversation_ids]
                        ),
                    ),
                )
            )
            .limit(1)
        )
        if deleted is not None:
            raise ContextSourceInvalidated()
