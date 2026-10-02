"""Offline whole-chat benchmark with fixed history/memory and a synthetic provider.

Compares budget enforcement on/off, keeping Run tracking and source guards in both.
This is not a pre-Harness baseline and does not measure HTTP, model or device latency.
Defaults to temporary SQLite. An explicitly supplied PostgreSQL _test database
uses owned temporary schemas, including pgvector, and removes them afterwards.
No existing configuration or provider client is opened; URLs never enter reports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chat import ChatService
from app.config import ConfigStore
from app.db import (
    AppUserRecord,
    ConversationRecord,
    Database,
    MessageRecord,
)
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult, LLMRoute, ModelUsage
from app.llm.router import LLMRouter
from app.memory import MemoryCandidate, MemoryStore, MemoryType, RuleBasedExtractor
from app.schemas import PrivacyLevel
from scripts.benchmark_harness import distribution
from scripts.benchmark_storage import FixtureStorage, open_storage, validate_test_url

TEXT = "请根据合成测试偏好简短回答。"
REPLY = "synthetic fixed reply"


class FixtureProvider:
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        assert len(request.messages) >= 8
        assert "合成测试偏好" in request.messages[0].content
        await asyncio.sleep(0)
        return CompletionResult(
            text=REPLY,
            provider="fixture",
            model="fixture",
            endpoint="fixture",
            route=request.route,
            latency_ms=0,
            usage=ModelUsage(input_tokens=128, output_tokens=8, total_tokens=136, usage_known=True),
        )

    async def stream(
        self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
    ) -> CompletionResult:
        result = await self.complete(request)
        await on_delta(result.text)
        return result

    async def probe(self) -> None:
        raise AssertionError("offline benchmark must never probe a provider")


@dataclass
class Case:
    database: Database
    storage: FixtureStorage
    service: ChatService
    user_id: UUID
    conversations: list[UUID]

    async def close(self) -> None:
        try:
            await self.service.drain_background_work()
        finally:
            await self.storage.close()


async def prepare(
    directory: Path, enabled: bool, count: int, postgres_test_url: str | None = None
) -> Case:
    directory.mkdir()
    storage = await open_storage(directory / "fixture.db", postgres_test_url)
    database = storage.database
    service: ChatService | None = None
    try:
        config_path = directory / "fixture.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "models": {
                        "fixture": {
                            "provider": "fixture",
                            "model": "fixture",
                            "base_url": "http://fixture.invalid",
                            "runs_local": True,
                            "max_privacy_level": "L2",
                            "supports_tool_calling": True,
                            "max_context_tokens": 32768,
                            "max_retries": 0,
                            "input_cost_per_million": 0,
                            "output_cost_per_million": 0,
                            "cost_currency": "USD",
                        }
                    },
                    "routes": {route.value: {"primary": "fixture"} for route in LLMRoute},
                    "run_budget": {"enabled": enabled, "max_concurrent_llm_calls": 32},
                }
            )
        )
        config = ConfigStore(config_path)
        await config.load()
        provider = FixtureProvider()
        memory = MemoryStore(database)
        service = ChatService(
            database,
            config,
            memory_store=memory,
            memory_extractor=RuleBasedExtractor(),
            router_builder=lambda value: LLMRouter(
                endpoints=value.models,
                routes=value.routes,
                providers={"fixture": provider},
            ),
        )
        user_id = uuid7()
        ids = [uuid7() for _ in range(count)]
        now = datetime.now(UTC)
        async with database.sessions.begin() as session:
            session.add(
                AppUserRecord(id=user_id, display_name="Synthetic fixture", status="active")
            )
            await session.flush()
            session.add_all(
                [
                    ConversationRecord(
                        id=value,
                        user_id=user_id,
                        title="Synthetic fixture",
                        status="active",
                        last_seq=6,
                        created_at=now,
                    )
                    for value in ids
                ]
            )
            await session.flush()
            session.add_all(
                [
                    MessageRecord(
                        id=uuid7(),
                        conversation_id=value,
                        turn_id=uuid7(),
                        seq=index + 1,
                        role="user" if index % 2 == 0 else "assistant",
                        content=f"synthetic fixed history {index}",
                        privacy_level="L1",
                        created_at=now,
                    )
                    for value in ids
                    for index in range(6)
                ]
            )
        for index in range(8):
            await memory.add(
                MemoryCandidate(
                    type=MemoryType.PREFERENCE,
                    content=f"合成测试偏好 {index}：喜欢简短回答",
                    privacy_level=PrivacyLevel.L1,
                    pin=index < 2,
                ),
                user_id=user_id,
            )
        return Case(database, storage, service, user_id, ids)
    except BaseException:
        try:
            if service is not None:
                await service.drain_background_work()
        finally:
            await storage.close()
        raise


async def benchmark(
    samples: int,
    concurrency: int,
    warmup: int,
    *,
    postgres_test_url: str | None = None,
) -> dict[str, Any]:
    if postgres_test_url is not None:
        validate_test_url(postgres_test_url)
    off_times: list[float] = []
    on_times: list[float] = []
    gate = asyncio.Semaphore(concurrency)
    verified = 0
    with tempfile.TemporaryDirectory(prefix="aria-chat-harness-") as temporary:
        off = await prepare(Path(temporary) / "off", False, samples + warmup, postgres_test_url)
        on: Case | None = None
        try:
            on = await prepare(Path(temporary) / "on", True, samples + warmup, postgres_test_url)

            async def turn(case: Case, index: int) -> float:
                nonlocal verified
                started = perf_counter_ns()
                result = await case.service.send_message(
                    case.conversations[index],
                    user_id=case.user_id,
                    text=TEXT,
                    privacy_level=PrivacyLevel.L1,
                )
                elapsed = (perf_counter_ns() - started) / 1_000_000
                assert result.assistant_message.content == REPLY
                run = await case.service.runs.get(
                    result.assistant_message.turn_id, user_id=case.user_id
                )
                assert run.status == "succeeded" and run.goal is not None
                assert run.goal.status == "passed"
                verified += 1
                return elapsed

            async def pair(index: int, record: bool) -> None:
                async with gate:
                    assert on is not None
                    if index % 2:
                        yes, no = await turn(on, index), await turn(off, index)
                    else:
                        no, yes = await turn(off, index), await turn(on, index)
                    if record:
                        off_times.append(no)
                        on_times.append(yes)

            async def pairs(indices: range, record: bool) -> None:
                tasks = [asyncio.create_task(pair(index, record)) for index in indices]
                try:
                    await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise

            await pairs(range(warmup), False)
            await off.service.drain_background_work()
            await on.service.drain_background_work()
            await pairs(range(warmup, samples + warmup), True)
        finally:
            try:
                if on is not None:
                    await on.close()
            finally:
                await off.close()
    increments = [yes - no for yes, no in zip(on_times, off_times, strict=True)]
    return {
        "schema_version": 1,
        "fixture": "whole-chat-history6-memory8-provider0-v1",
        "synthetic": True,
        "observed_at": datetime.now(UTC).isoformat(),
        "environment": {
            "os": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "logical_cpus": os.cpu_count(),
            "database": "isolated PostgreSQL schemas with pgvector"
            if postgres_test_url is not None
            else "isolated file SQLite",
            "database_version": off.storage.server_version,
        },
        "samples_per_mode": samples,
        "warmup_per_mode": warmup,
        "concurrency": concurrency,
        "verified_commits": verified,
        "scope": "ChatService acceptance, context assembly, source checks, Router/window, "
        "Run/budget, "
        "reply commit and postcommit enqueue; excludes HTTP/network/model/device latency "
        "and waiting for postcommit completion. Concurrent background work may contend.",
        "comparison": "budget on/off; both retain Harness loop, Run tracking and source guards; "
        "not a pre-Harness whole-chat baseline",
        "budget_off": distribution(off_times),
        "budget_on": distribution(on_times),
        "paired_budget_increment": distribution(increments),
        "raw_ms": {
            "budget_off": off_times,
            "budget_on": on_times,
            "paired_budget_increment": increments,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--postgres-test-url", help="Explicit asyncpg URL ending in _test")
    args = parser.parse_args()
    if (
        not 10 <= args.samples <= 1000
        or not 0 <= args.warmup <= 100
        or not 1 <= args.concurrency <= 8
    ):
        parser.error("samples 10..1000, warmup 0..100, concurrency 1..8")
    if args.postgres_test_url is not None:
        try:
            validate_test_url(args.postgres_test_url)
        except ValueError:
            parser.error("PostgreSQL fixture requires asyncpg and a dedicated _test database")
    report = asyncio.run(
        benchmark(
            args.samples, args.concurrency, args.warmup, postgres_test_url=args.postgres_test_url
        )
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    report.pop("raw_ms")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
