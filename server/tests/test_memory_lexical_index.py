"""词法索引（方案 B1）：FTS5 同步、两段式候选与旧路径严格等价。

等价契约：开关开/关两路对同一查询产出**完全相同的候选 ID 序列**
（热层+补齐拼接序），索引只改速度不改结果。迁移链测试另证
0073 升级回填与降级。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app.db import Database
from app.memory import MemoryRetriever, MemoryStore
from app.schemas.common import PrivacyLevel
from scripts.eval_memory_retrieval import build_corpus


@pytest.fixture(params=["sqlite", "postgresql"])
async def database(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Database]:
    from test_run_cancel_fence import prepared

    storage = await prepared(request.param, tmp_path)
    try:
        yield storage.database
    finally:
        await storage.close()


async def _owner(database: Database) -> UUID:
    from scripts.eval_memory_retrieval import _new_user

    return await _new_user(database, "lex-fixture")


_QUERIES = (
    "小晴的生日是哪天",
    "用户住在哪座城市",
    "用户平时怎么锻炼身体",
    "点菜时能吃香菜吗",
    "用户周末有什么爱好活动",
    "用户以前玩过狼人杀吗",
)


async def test_indexed_candidates_match_legacy_ordering(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await _owner(database)
    store = MemoryStore(database)
    await build_corpus(store, owner)

    async def run(mode: str) -> list[list[int]]:
        from datetime import UTC, datetime

        monkeypatch.setenv("ARIA_MEMORY_LEXICAL_INDEX", mode)
        from app.memory.retrieval import DEFAULT_SUBJECT_SCOPES
        from app.schemas.common import persistent_privacy_levels

        moment = datetime.now(UTC)
        result: list[list[int]] = []
        for query in _QUERIES:
            for deep in (False, True):
                rows = await store.retrieval_candidates(
                    owner,
                    subject_scopes=DEFAULT_SUBJECT_SCOPES,
                    privacy_levels=persistent_privacy_levels(PrivacyLevel.L1),
                    valid_at=moment,
                    limit=500,
                    query_text=query,
                    deep=deep,
                )
                result.append([item.entry.id for item in rows])
        return result

    legacy = await run("0")
    indexed = await run("1")
    assert indexed == legacy
    # 端到端检索命中同样保持一致
    monkeypatch.setenv("ARIA_MEMORY_LEXICAL_INDEX", "0")
    legacy_hits = [
        [
            hit.memory.id
            for hit in (
                await MemoryRetriever(store).retrieve(
                    query, user_id=owner, privacy_level=PrivacyLevel.L1
                )
            ).hits
        ]
        for query in _QUERIES
    ]
    monkeypatch.setenv("ARIA_MEMORY_LEXICAL_INDEX", "1")
    indexed_hits = [
        [
            hit.memory.id
            for hit in (
                await MemoryRetriever(store).retrieve(
                    query, user_id=owner, privacy_level=PrivacyLevel.L1
                )
            ).hits
        ]
        for query in _QUERIES
    ]
    assert indexed_hits == legacy_hits


async def test_write_paths_keep_fts_in_sync(database: Database) -> None:
    if database.engine.dialect.name != "sqlite":
        pytest.skip("FTS row sync is sqlite-only")
    owner = await _owner(database)
    store = MemoryStore(database)
    corpus = await build_corpus(store, owner)

    async def lex_ids() -> set[int]:
        async with database.sessions() as session:
            rows = await session.execute(text("SELECT rowid FROM memory_lex"))
            return {int(row[0]) for row in rows}

    async def memory_ids() -> set[int]:
        async with database.sessions() as session:
            rows = await session.execute(text("SELECT id FROM memory"))
            return {int(row[0]) for row in rows}

    assert await lex_ids() == await memory_ids()
    # 纠错生成替代版本 → 新行进入索引，旧行保留（superseded 由查询过滤）
    replacement = await store.edit(
        corpus.labels["city"],
        user_id=owner,
        content="用户住在青岛，市北区",
        actor="fixture",
        reason="fixture correction",
    )
    assert await lex_ids() == await memory_ids()
    # 硬删除整条链 → FTS 行同步消失
    await store.hard_delete(replacement.id, actor="fixture", reason="fixture")
    assert await lex_ids() == await memory_ids()
    # 索引内容可命中：替代版本尚未删除时，改写后的关键词能召回
    second = await store.edit(
        corpus.labels["job"], user_id=owner, content="用户转行做餐饮管理", actor="f", reason="f"
    )
    hits = await MemoryRetriever(store).retrieve(
        "用户做餐饮管理吗", user_id=owner, privacy_level=PrivacyLevel.L1
    )
    assert second.id in {hit.memory.id for hit in hits.hits}
    await store.hard_delete(second.id, actor="fixture", reason="fixture")


def test_migration_0073_backfills_fts_and_downgrades(tmp_path: Path) -> None:
    path = tmp_path / "lex-migration.db"
    environment = {**os.environ, "ARIA_DATABASE_URL": f"sqlite+aiosqlite:///{path}"}

    def migrate(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    assert migrate("upgrade", "0072_memory_lifecycle").returncode == 0
    from app.ids import uuid7

    with sqlite3.connect(path) as connection:
        owner = uuid7().hex
        connection.execute("INSERT INTO app_user(id,display_name) VALUES (?, 'Fixture')", (owner,))
        for index in range(3):
            connection.execute(
                "INSERT INTO memory(user_id,type,content,privacy_level) VALUES "
                f"(?,'semantic','合成迁移事实编号{index}','L1')",
                (owner,),
            )
    assert migrate("upgrade", "head").returncode == 0
    with sqlite3.connect(path) as connection:
        counted = connection.execute("SELECT COUNT(*) FROM memory_lex").fetchone()[0]
        assert counted == 3
        grams = connection.execute("SELECT grams FROM memory_lex LIMIT 1").fetchone()[0]
        assert "合成" in grams and "迁移" in grams  # 一元/二元 gram 均已入库
        assert migrate("downgrade", "0072_memory_lifecycle").returncode == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='memory_lex'"
            ).fetchone()[0]
            == 0
        )
