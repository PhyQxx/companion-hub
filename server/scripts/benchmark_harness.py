"""Offline microbenchmark: fixed stub completion and isolated durable cancellation.

No configuration secrets, provider clients, production database or device calls.
The comparison measures only zero-tool AgentLoop overhead, not whole-chat overhead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import sys
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import AppUserRecord, Base, TaskRunRecord, create_database
from app.harness.loop import CompletionFrame, run_agent_loop
from app.ids import uuid7
from app.llm.contracts import (
    CompletionRequest,
    CompletionResult,
    LLMMessage,
    LLMRoute,
)
from app.runs.store import RunStore
from app.schemas import PrivacyLevel


def distribution(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        name: round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 6)
        for name, fraction in (("p50_ms", 0.5), ("p95_ms", 0.95), ("p99_ms", 0.99))
    }


async def timed(operation: Callable[[], Awaitable[object]]) -> float:
    started = perf_counter_ns()
    await operation()
    return (perf_counter_ns() - started) / 1_000_000


async def benchmark(samples: int, concurrency: int, warmup: int) -> dict[str, Any]:
    request = CompletionRequest(
        trace_id=uuid7(),
        messages=[LLMMessage(role="user", content="synthetic fixed fixture")],
        privacy_level=PrivacyLevel.L1,
        route=LLMRoute.DIALOGUE,
    )
    result = CompletionResult(
        text="synthetic reply",
        provider="fixture",
        model="fixture",
        endpoint="fixture",
        route=LLMRoute.DIALOGUE,
        latency_ms=0,
    )

    async def complete(value: CompletionRequest, final: bool) -> CompletionFrame:
        # Yield once so the fixture includes cooperative scheduling.
        await asyncio.sleep(0)
        return CompletionFrame(result)

    async def execute(calls: object) -> list[object]:
        raise AssertionError("zero-tool fixture must not execute tools")

    async def baseline() -> object:
        return await complete(request, True)

    async def harness() -> object:
        return await run_agent_loop(
            request,
            max_tool_rounds=4,
            complete=complete,
            normalize=lambda req, res: res,
            execute=execute,
            followup=lambda req, res, work, more: req,
            direct_reply=lambda work: None,
            check_cancelled=lambda: None,
        )

    baseline_times: list[float] = []
    harness_times: list[float] = []
    gate = asyncio.Semaphore(concurrency)

    async def pair(index: int, record: bool) -> None:
        async with gate:
            # Alternate order to reduce warm-cache/order bias.
            if index % 2:
                h, b = await timed(harness), await timed(baseline)
            else:
                b, h = await timed(baseline), await timed(harness)
            if record:
                baseline_times.append(b)
                harness_times.append(h)

    await asyncio.gather(*(pair(index, False) for index in range(warmup)))
    await asyncio.gather(*(pair(index, True) for index in range(samples)))
    cancellation_times: list[float] = []
    with tempfile.TemporaryDirectory(prefix="aria-harness-benchmark-") as directory:
        database = create_database(f"sqlite+aiosqlite:///{directory}/fixture.db")
        try:
            async with database.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            owner = uuid7()
            now = datetime.now(UTC)
            run_ids = [uuid7() for _ in range(samples + warmup)]
            async with database.sessions.begin() as session:
                session.add(
                    AppUserRecord(id=owner, display_name="Synthetic benchmark", status="active")
                )
                session.add_all(
                    [
                        TaskRunRecord(
                            id=run_id,
                            user_id=owner,
                            status="running",
                            privacy_level="L1",
                            contract={"criterion": "reply_committed", "required_work": []},
                            created_at=now,
                            updated_at=now,
                        )
                        for run_id in run_ids
                    ]
                )
            store = RunStore(database)

            async def cancel(index: int) -> None:
                async with gate:
                    elapsed = await timed(lambda: store.cancel_work(run_ids[index], user_id=owner))
                    if index >= warmup:
                        cancellation_times.append(elapsed)

            await asyncio.gather(*(cancel(index) for index in range(warmup)))
            await asyncio.gather(*(cancel(index) for index in range(warmup, len(run_ids))))
        finally:
            await database.close()
    delta = [h - b for h, b in zip(harness_times, baseline_times, strict=True)]
    return {
        "schema_version": 1,
        "fixture": "zero-tool-loop-and-empty-run-cancel-v1",
        "observed_at": datetime.now(UTC).isoformat(),
        "synthetic": True,
        "environment": {
            "os": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "logical_cpus": os.cpu_count(),
            "database": "isolated SQLite",
        },
        "samples": samples,
        "warmup": warmup,
        "concurrency": concurrency,
        "scope": (
            "zero-tool loop incremental overhead and empty-run cancellation admission; "
            "excludes whole chat, tools, network, model and device latency"
        ),
        "baseline": distribution(baseline_times),
        "harness": distribution(harness_times),
        "paired_increment": distribution(delta),
        "cancellation": distribution(cancellation_times),
        "targets": {"loop_increment_p95_ms": 50, "cancellation_p95_ms": 1000},
        "targets_met": {
            "loop_increment": distribution(delta)["p95_ms"] <= 50,
            "cancellation": distribution(cancellation_times)["p95_ms"] <= 1000,
        },
        "raw_ms": {
            "baseline": baseline_times,
            "harness": harness_times,
            "paired_increment": delta,
            "cancellation": cancellation_times,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (
        not 10 <= args.samples <= 10000
        or not 0 <= args.warmup <= 1000
        or not 1 <= args.concurrency <= 32
    ):
        parser.error("samples 10..10000, warmup 0..1000, concurrency 1..32")
    report = asyncio.run(benchmark(args.samples, args.concurrency, args.warmup))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    report.pop("raw_ms")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
