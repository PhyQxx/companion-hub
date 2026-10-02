from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.db import Database
from app.memory.store import MemoryStore
from app.persona import PersonaStore
from app.privacy.deletion_journal import DeletionJournal
from app.timeline import TimelineStore


@dataclass(frozen=True, slots=True)
class MemoryModule:
    persona: PersonaStore | None
    memory: MemoryStore | None
    timeline: TimelineStore | None


def build_memory(database: Database | None, *, journal_path: Path | None = None) -> MemoryModule:
    return MemoryModule(
        persona=PersonaStore(database) if database is not None else None,
        memory=MemoryStore(
            database, deletion_journal=DeletionJournal(journal_path) if journal_path else None
        )
        if database is not None
        else None,
        timeline=TimelineStore(database) if database is not None else None,
    )
