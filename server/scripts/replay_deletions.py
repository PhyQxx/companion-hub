"""Offline deletion replay for an isolated restore; dry-run by default."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from app.db import create_database  # noqa: E402
from app.memory import MemoryStore  # noqa: E402
from app.memory.replay import (  # noqa: E402
    _replay_memory_row,
    _replay_message_row,
    import_deletion_journal,
    replay_deletions,
)
from app.privacy.deletion_journal import DeletionJournal  # noqa: E402


async def run(*, apply: bool, journal_path: Path | None) -> None:
    url = os.getenv("ARIA_DATABASE_URL")
    if not url:
        raise ValueError("ARIA_DATABASE_URL_required")
    database = create_database(url)
    try:
        journal = DeletionJournal(journal_path) if journal_path else None
        imported = 0
        preview = {"intents": 0, "conversations": 0, "memories": 0}
        if journal is not None:
            intents = await journal.read()
            preview["intents"] = len(intents)
            if apply:
                imported = await import_deletion_journal(database, journal)
            else:
                store = MemoryStore(database)
                for intent in intents:
                    if intent.entity_kind == "memory":
                        preview["memories"] += await _replay_memory_row(
                            store, tuple(intent.deleted_ids), True
                        )
                    else:
                        conversations, memories = await _replay_message_row(
                            database,
                            store,
                            intent.entity_id,
                            True,
                            deleted_ids=tuple(intent.deleted_ids),
                        )
                        preview["conversations"] += conversations
                        preview["memories"] += memories
        report = await replay_deletions(database, dry_run=not apply)
        # Separate previews can overlap; never present their sum as distinct objects.
        print(
            json.dumps({"journal_preview": preview, "imported": imported, "ledger": asdict(report)})
        )
    finally:
        await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Apply deletion intent to the target DB"
    )
    parser.add_argument("--journal", type=Path, help="Original installation's latest journal.jsonl")
    arguments = parser.parse_args()
    asyncio.run(run(apply=arguments.apply, journal_path=arguments.journal))
