"""Synthetic SQLite candidate-search baseline, never touches a configured user DB."""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import insert

from app.db import AppUserRecord, Base, MemoryRecord, create_database
from app.memory import MemoryRetriever, MemoryStore
from app.schemas import PrivacyLevel


async def run(path: Path, sizes: list[int]) -> None:
    if not path.name.endswith("_test.db") or path.exists():
        raise SystemExit("Use a new path ending in _test.db")
    database = create_database(f"sqlite+aiosqlite:///{path.resolve()}")
    owner = uuid4()
    try:
        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                insert(AppUserRecord),
                [{"id": owner, "display_name": "benchmark", "status": "active", "timezone": "UTC"}],
            )
        previous = 0
        results = []
        for size in sizes:
            for start in range(previous, size, 5000):
                rows = [
                    {
                        "user_id": owner,
                        "type": "semantic",
                        "content": f"Synthetic memory {index} project record",
                        "privacy_level": "L1",
                        "tier": "medium" if index % 2 else "dormant",
                    }
                    for index in range(start, min(start + 5000, size))
                ]
                async with database.engine.begin() as conn:
                    await conn.execute(insert(MemoryRecord), rows)
            samples = []
            retriever = MemoryRetriever(MemoryStore(database))
            for _ in range(5):
                started = time.perf_counter()
                await retriever.retrieve(
                    "project record", user_id=owner, privacy_level=PrivacyLevel.L1
                )
                samples.append(round((time.perf_counter() - started) * 1000, 2))
            results.append(
                {
                    "rows": size,
                    "samples_ms": samples,
                    "median_ms": sorted(samples)[2],
                    "max_ms": max(samples),
                }
            )
            print(json.dumps(results[-1]), flush=True)
            previous = size
        path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "measured_at": datetime.now(UTC).isoformat(),
                    "scope": (
                        "synthetic SQLite without embeddings; "
                        "not PostgreSQL ANN or semantic accuracy"
                    ),
                    "results": results,
                },
                indent=2,
            )
        )
    finally:
        await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--sizes", default="10000,100000,1000000")
    args = parser.parse_args()
    asyncio.run(run(args.path, [int(value) for value in args.sizes.split(",")]))
