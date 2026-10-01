from __future__ import annotations

from dataclasses import dataclass

from app.db import Database
from app.memory.store import MemoryStore
from app.persona import PersonaStore
from app.timeline import TimelineStore


@dataclass(frozen=True, slots=True)
class MemoryModule:
    persona: PersonaStore | None
    memory: MemoryStore | None
    timeline: TimelineStore | None


def build_memory(database: Database | None) -> MemoryModule:
    return MemoryModule(
        persona=PersonaStore(database) if database is not None else None,
        memory=MemoryStore(database) if database is not None else None,
        timeline=TimelineStore(database) if database is not None else None,
    )
