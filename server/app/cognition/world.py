"""Compatibility composition for callers that still supply a database."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .world_assembly import WorldAssembler
from .world_facts import WorldFactsRepository
from .world_ports import GoalRepository, MemoryQuerySource, TimelineQuerySource

if TYPE_CHECKING:
    from app.db import Database


class WorldStateBuilder(WorldAssembler):
    def __init__(
        self,
        database: Database,
        store: GoalRepository,
        *,
        memory_retriever: MemoryQuerySource | None = None,
        timeline_store: TimelineQuerySource | None = None,
        repository: WorldFactsRepository | None = None,
    ) -> None:
        if repository is None:
            from .world_sql import SqlWorldFactsRepository

            repository = SqlWorldFactsRepository(database)
        super().__init__(
            repository, store, memory_retriever=memory_retriever, timeline_store=timeline_store
        )
