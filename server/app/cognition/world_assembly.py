"""World assembly over explicit domain repositories and detached query results."""

from __future__ import annotations

from datetime import UTC, datetime

from app.context.snapshots import memory_reference, timeline_reference
from app.harness.context import ContextReference
from app.schemas import PrivacyLevel
from app.schemas.common import persistent_privacy_levels

from .models import SemanticEvent, WorldState
from .world_facts import WorldFactsRepository
from .world_ports import GoalRepository, MemoryQuerySource, TimelineQuerySource


class WorldAssembler:
    def __init__(
        self,
        repository: WorldFactsRepository,
        store: GoalRepository,
        *,
        memory_retriever: MemoryQuerySource | None = None,
        timeline_store: TimelineQuerySource | None = None,
    ) -> None:
        self._repository = repository
        self._store = store
        self._memory_retriever = memory_retriever
        self._timeline_store = timeline_store

    async def build(self, event: SemanticEvent, *, now: datetime | None = None) -> WorldState:
        event = event.model_copy(deep=True)
        moment = now or datetime.now(UTC)
        visible_levels = persistent_privacy_levels(event.privacy_level)
        facts = await self._repository.read(
            user_id=event.user_id,
            trigger_kind=event.kind,
            privacy_level=event.privacy_level,
            now=moment,
        )
        references: tuple[ContextReference, ...] = ()
        memory_ids: list[str] = []
        if self._memory_retriever is not None and event.privacy_level != PrivacyLevel.L3:
            try:
                memory_result = await self._memory_retriever.retrieve(
                    event.summary,
                    user_id=event.user_id,
                    privacy_level=event.privacy_level,
                    now=moment,
                )
                selected = memory_result.hits[:4]
                memory_references = tuple(memory_reference(hit, included=True) for hit in selected)
                memory_references = await self._repository.attach_memory_lineage(
                    memory_references, user_id=event.user_id
                )
                references += memory_references
                memory_ids = [str(hit.memory.id) for hit in selected]
            except Exception:
                memory_ids = []
        timeline_ids: list[str] = []
        if self._timeline_store is not None and event.privacy_level != PrivacyLevel.L3:
            try:
                timeline_result = await self._timeline_store.search(
                    user_id=event.user_id,
                    query=event.summary,
                    privacy_levels=visible_levels,
                    limit=4,
                )
                references += tuple(timeline_reference(item) for item in timeline_result.events)
                timeline_ids = [str(item.id) for item in timeline_result.events]
            except Exception:
                timeline_ids = []
        return WorldState(
            built_at=moment,
            timezone=facts.timezone or "Asia/Shanghai",
            last_interaction_at=facts.last_interaction_at,
            active_capabilities=list(facts.active_capabilities),
            active_goals=await self._store.active_goals(
                event.user_id, now=moment, max_privacy_level=event.privacy_level
            ),
            context_references=references,
            memory_evidence_ids=memory_ids,
            timeline_evidence_ids=timeline_ids,
            recent_proactive_count=facts.recent_proactive_count,
            same_trigger_recent_count=facts.same_trigger_recent_count,
            ignored_same_trigger_count=facts.ignored_same_trigger_count,
            dnd=bool(event.attributes.get("dnd", False)),
        )
