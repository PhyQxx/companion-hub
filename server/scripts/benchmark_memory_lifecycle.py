"""Synthetic SQLite candidate-search baseline, never touches a configured user DB."""

import argparse
import asyncio
import json
import os
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
        last_lex_id = 0
        results = []
        for size in sizes:
            for start in range(previous, size, 5000):
                rows = [
                    {
                        "user_id": owner,
                        "type": "semantic",
                        "content": (
                            # 每 10 万行埋一条稀疏标记行：测"少数行命中"的真实场景，
                            # 其余行为密命中场景（旧行为对照保留）。
                            f"用户在崂山看了日出的特别记忆 {index}"
                            if index % 100_000 == 7
                            else f"Synthetic memory {index} project record"
                        ),
                        "privacy_level": "L1",
                        "tier": "medium" if index % 2 else "dormant",
                    }
                    for index in range(start, min(start + 5000, size))
                ]
                async with database.engine.begin() as conn:
                    await conn.execute(insert(MemoryRecord), rows)
            # 方案 B1：批量填充词法索引（与迁移 0073 同口径），双路测速。
            from sqlalchemy import text

            from app.memory.similarity import text_tokens

            async with database.engine.begin() as conn:
                await conn.execute(
                    text("CREATE VIRTUAL TABLE IF NOT EXISTS memory_lex USING fts5(grams)")
                )
            while True:
                async with database.sessions() as session:
                    batch = (
                        await session.execute(
                            text(
                                "SELECT id, content FROM memory WHERE id > :last"
                                " ORDER BY id LIMIT 5000"
                            ),
                            {"last": last_lex_id},
                        )
                    ).all()
                if not batch:
                    break
                async with database.engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT OR REPLACE INTO memory_lex(rowid, grams) VALUES (:id, :grams)"
                        ),
                        [
                            {"id": row[0], "grams": " ".join(text_tokens(row[1] or ""))}
                            for row in batch
                        ],
                    )
                last_lex_id = batch[-1][0]
            entry: dict[str, object] = {"rows": size}
            for query_label, query_text in (
                ("dense", "project record"),
                ("sparse", "崂山日出"),
            ):
                for mode in ("legacy_scan", "lexical_index"):
                    os.environ["ARIA_MEMORY_LEXICAL_INDEX"] = "0" if mode == "legacy_scan" else "1"
                    retriever = MemoryRetriever(MemoryStore(database))
                    samples = []
                    for _ in range(5):
                        started = time.perf_counter()
                        await retriever.retrieve(
                            query_text, user_id=owner, privacy_level=PrivacyLevel.L1
                        )
                        samples.append(round((time.perf_counter() - started) * 1000, 2))
                    entry[f"{query_label}_{mode}"] = {
                        "samples_ms": samples,
                        "median_ms": sorted(samples)[2],
                        "max_ms": max(samples),
                    }
            results.append(entry)
            print(json.dumps(results[-1]), flush=True)
            previous = size
        os.environ.pop("ARIA_MEMORY_LEXICAL_INDEX", None)
        path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "measured_at": datetime.now(UTC).isoformat(),
                    "scope": (
                        "synthetic SQLite without embeddings; legacy scan vs"
                        " FTS5 lexical index (B1); not PostgreSQL ANN or"
                        " semantic accuracy"
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
