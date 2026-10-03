"""Content-free references from detached domain views; no database access."""

from datetime import UTC, datetime

from app.harness.context import ContextReference
from app.memory.retrieval_models import MemoryHit
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
