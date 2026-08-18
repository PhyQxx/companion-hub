from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from app.db import create_database  # noqa: E402
from app.memory import replay_deletions  # noqa: E402


async def main() -> None:
    database_url = os.getenv("ARIA_DATABASE_URL")
    if not database_url:
        raise SystemExit("ARIA_DATABASE_URL is not configured")
    dry_run = "--apply" not in sys.argv
    database = create_database(database_url)
    try:
        report = await replay_deletions(database, dry_run=dry_run)
    finally:
        await database.close()
    print(json.dumps(report.__dict__, ensure_ascii=False, indent=2))
    if dry_run:
        print("dry run only; pass --apply to re-apply the deletion ledger")


if __name__ == "__main__":
    asyncio.run(main())
