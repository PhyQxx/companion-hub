"""Translate authorized domain snapshots to content-free harness references."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    ConversationRecord,
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
    rows = await session.scalars(
        select(MemorySourceRecord)
        .join(
            MemoryRecord,
            MemoryRecord.id == MemorySourceRecord.memory_id,
        )
        .where(MemoryRecord.user_id == owner_id, MemorySourceRecord.memory_id.in_(ids))
    )
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
                ConversationRecord, ConversationRecord.id == MessageRecord.conversation_id
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
                if isinstance(row, MemoryRecord) and row.status != "active":
                    raise ContextSourceInvalidated()
