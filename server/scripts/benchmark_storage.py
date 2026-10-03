"""Owned fixture storage; PostgreSQL requires an explicit dedicated test URL."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url

from app.db import Base, Database, create_database
from app.ids import uuid7


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    fingerprint: str
    file_count: int
    scope: str = "server/app and server/scripts Python files"

    def fields(self) -> dict[str, str | int]:
        return {
            "source_fingerprint": self.fingerprint,
            "source_file_count": self.file_count,
            "source_scope": self.scope,
        }


def source_provenance(root: Path | None = None) -> SourceProvenance:
    root = root or Path(__file__).resolve().parents[1]
    paths = sorted((*root.joinpath("app").rglob("*.py"), *root.joinpath("scripts").rglob("*.py")))
    if not paths:
        raise ValueError("benchmark_source_missing")
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return SourceProvenance(digest.hexdigest(), len(paths))


def validate_test_url(url: str) -> None:
    parsed = make_url(url)
    if parsed.drivername != "postgresql+asyncpg" or not (parsed.database or "").endswith("_test"):
        raise ValueError("PostgreSQL fixture requires asyncpg and a dedicated _test database")


@dataclass
class FixtureStorage:
    database: Database
    schema: str | None = None
    search_path: Any = None
    server_version: str | None = None

    async def close(self) -> None:
        try:
            if self.schema is not None:
                if event.contains(self.database.engine.sync_engine, "checkout", self.search_path):
                    event.remove(self.database.engine.sync_engine, "checkout", self.search_path)
                self.database.engine.update_execution_options(schema_translate_map=None)
                async with self.database.engine.begin() as connection:
                    await connection.exec_driver_sql(f"DROP SCHEMA {self.schema} CASCADE")
        finally:
            await self.database.close()


async def open_storage(path: Path, postgres_test_url: str | None = None) -> FixtureStorage:
    if postgres_test_url is not None:
        validate_test_url(postgres_test_url)
    storage = FixtureStorage(create_database(postgres_test_url or f"sqlite+aiosqlite:///{path}"))
    try:
        if postgres_test_url is not None:
            schema = f"aria_chat_fixture_{uuid7().hex}"
            async with storage.database.engine.begin() as connection:
                await connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
                storage.schema = schema
                storage.server_version = str(
                    (await connection.exec_driver_sql("SHOW server_version")).scalar_one()
                )

            def search_path(connection: Any, record: Any, proxy: Any) -> None:
                cursor = connection.cursor()
                try:
                    cursor.execute(f"SET search_path TO {schema}, public")
                finally:
                    cursor.close()

            storage.search_path = search_path
            event.listen(storage.database.engine.sync_engine, "checkout", search_path)
            storage.database.engine.update_execution_options(schema_translate_map={None: schema})
        async with storage.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            if storage.schema is not None:
                # The test database must already provide pgvector. Create only
                # the migration's vector artifacts in this fixture's own schema.
                await connection.exec_driver_sql(
                    f"ALTER TABLE {storage.schema}.memory ADD COLUMN embedding_vec vector(256)"
                )
                await connection.exec_driver_sql(
                    f"CREATE INDEX ix_memory_embedding_vec ON {storage.schema}.memory"
                    " USING hnsw (embedding_vec vector_cosine_ops)"
                )
        return storage
    except BaseException:
        await storage.close()
        raise
