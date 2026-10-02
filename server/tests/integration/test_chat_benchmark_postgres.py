"""Exercise real pgvector chat commits in owned schemas, including failure cleanup."""

import asyncio
import os
from typing import Any

import pytest

from app.db import Database, create_database
from app.llm import CompletionRequest, CompletionResult, LLMRouteExhausted
from scripts.benchmark_chat_harness import FixtureProvider, benchmark
from scripts.benchmark_storage import validate_test_url


async def schemas(database: Database) -> set[str]:
    async with database.engine.connect() as connection:
        result = await connection.exec_driver_sql(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'aria_chat_fixture_%'"
        )
        return set(result.scalars())


@pytest.mark.parametrize("fail", [False, True])
async def test_postgres_chat_fixture_cleans_owned_schemas(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    validate_test_url(url)
    database = create_database(url)
    before = await schemas(database)
    started, cancelled = asyncio.Event(), asyncio.Event()
    calls = 0
    if fail:

        async def broken(self: Any, request: CompletionRequest) -> CompletionResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.wait_for(started.wait(), 3)
                raise RuntimeError("fixture_provider_failed")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("unreachable")

        monkeypatch.setattr(FixtureProvider, "complete", broken)
    try:
        if fail:
            with pytest.raises(LLMRouteExhausted):
                await benchmark(samples=2, concurrency=2, warmup=0, postgres_test_url=url)
            assert started.is_set() and cancelled.is_set()
        else:
            report = await benchmark(samples=2, concurrency=2, warmup=1, postgres_test_url=url)
            assert report["verified_commits"] == 6
            assert report["environment"]["database"] == "isolated PostgreSQL schemas with pgvector"
            assert report["environment"]["database_version"]
            assert url not in str(report) and "harness-test-only" not in str(report)
        assert await schemas(database) == before
    finally:
        await database.close()
