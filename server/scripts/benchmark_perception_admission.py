"""Offline rule ingress admission overhead; never opens models or live sources.

Compare the previous store/rule path with durable admission, on isolated storage.
This is an IGNORE-event fixture, not model, notification or deployment latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cognition import (
    AttentionEngine,
    CognitiveCycle,
    CognitiveStore,
    DecisionKind,
    RuleBasedDeliberator,
    SemanticEvent,
    WorldStateBuilder,
)
from app.db import AppUserRecord, CognitiveDecisionRecord, JobRecord, SemanticEventAuditRecord
from app.ids import uuid7
from app.perception import (
    PerceptionDisposition,
    PerceptionPipeline,
    PerceptionStore,
    ProactivePolicy,
    ProactivePolicySettings,
)
from app.schemas import PrivacyLevel
from scripts.benchmark_harness import distribution
from scripts.benchmark_storage import (
    FixtureStorage,
    open_storage,
    source_provenance,
    validate_test_url,
)


async def run_case(
    storage: FixtureStorage, *, samples: int, warmup: int, concurrency: int, admitted: bool
) -> list[float]:
    database = storage.database
    owner = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=owner, display_name="Synthetic benchmark", status="active"))
    cognitive = CognitiveStore(database)
    pipeline = PerceptionPipeline(
        CognitiveCycle(
            cognitive,
            WorldStateBuilder(database, cognitive),
            AttentionEngine(),
            RuleBasedDeliberator(),
        ),
        PerceptionStore(database),
        ProactivePolicy(
            database, ProactivePolicySettings(quiet_hours_start="00:00", quiet_hours_end="00:00")
        ),
    )
    gate = asyncio.Semaphore(concurrency)

    async def turn(index: int) -> float:
        async with gate:
            now = datetime.now(UTC)
            event = SemanticEvent(
                event_id=uuid7(),
                user_id=owner,
                kind="benchmark_probe",
                source_kind="synthetic_benchmark",
                dedupe_key=f"fixture:{index}",
                summary="Synthetic empty-world rule probe",
                occurred_at=now,
                expires_at=now + timedelta(minutes=5),
                privacy_level=PrivacyLevel.L1,
                evidence_ids=["synthetic:probe"],
            )
            started = perf_counter_ns()
            if admitted:
                result = await pipeline.process(event)
            else:
                # Deliberately exercise the prior path only inside this offline
                # fixture. This is not a production switch or an auth bypass API.
                result = await pipeline._process_locked(
                    event, key=(owner, pipeline._dedupe_key(event))
                )
            elapsed = (perf_counter_ns() - started) / 1_000_000
            assert result.disposition == PerceptionDisposition.PROCESSED
            assert result.decision is not None and result.decision.decision == DecisionKind.IGNORE
            return elapsed

    async def group(indices: range) -> list[float]:
        tasks = [asyncio.create_task(turn(index)) for index in indices]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    try:
        await group(range(warmup))
        timings = await group(range(warmup, samples + warmup))
        async with database.sessions() as session:
            for model in (CognitiveDecisionRecord, SemanticEventAuditRecord):
                assert (
                    await session.scalar(select(func.count()).select_from(model))
                    == samples + warmup
                )
            jobs = list(await session.scalars(select(JobRecord)))
        assert len(jobs) == (samples + warmup if admitted else 0)
        assert all(job.status == "succeeded" and job.attempts == 1 for job in jobs)
        return timings
    finally:
        await pipeline.stop()


async def benchmark(
    samples: int, concurrency: int, warmup: int, postgres_test_url: str | None = None
) -> dict[str, Any]:
    if not 1 <= samples <= 1000 or not 1 <= concurrency <= 8 or not 0 <= warmup <= 100:
        raise ValueError("invalid benchmark dimensions")
    if postgres_test_url is not None:
        validate_test_url(postgres_test_url)
    provenance = source_provenance()
    values: dict[str, list[float]] = {}
    version = None
    with tempfile.TemporaryDirectory(prefix="aria-perception-benchmark-") as directory:
        for admitted in (False, True):
            storage = await open_storage(
                Path(directory) / f"fixture-{admitted}.db", postgres_test_url
            )
            try:
                values["admitted" if admitted else "previous_path"] = await run_case(
                    storage,
                    samples=samples,
                    warmup=warmup,
                    concurrency=concurrency,
                    admitted=admitted,
                )
                version = storage.server_version
            finally:
                await storage.close()
    increments = [
        yes - no for yes, no in zip(values["admitted"], values["previous_path"], strict=True)
    ]
    if provenance != source_provenance():
        raise RuntimeError("benchmark_source_changed")
    return {
        "schema_version": 1,
        "fixture": "perception-empty-world-ignore-rule-v1",
        "synthetic": True,
        **provenance.fields(),
        "observed_at": datetime.now(UTC).isoformat(),
        "samples_per_mode": samples,
        "warmup_per_mode": warmup,
        "concurrency": concurrency,
        "verified_decisions": 2 * (samples + warmup),
        "environment": {
            "os": platform.system(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "logical_cpus": os.cpu_count(),
            "database": "isolated PostgreSQL schemas"
            if postgres_test_url
            else "isolated file SQLite",
            "database_version": version,
        },
        "scope": (
            "policy/store/world/rule/decision/audit commit; admission adds Job ownership, "
            "fingerprint, dedupe reservation and claim guards; excludes models, observers, "
            "handlers, HTTP, devices and notification delivery"
        ),
        "comparison": (
            "separate fixture phases and sample-index differences; not alternating "
            "same-request pairs or total Harness overhead"
        ),
        "previous_path": distribution(values["previous_path"]),
        "admitted": distribution(values["admitted"]),
        "sample_index_increment": distribution(increments),
        "raw_ms": {**values, "sample_index_increment": increments},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--postgres-test-url")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(
        benchmark(args.samples, args.concurrency, args.warmup, args.postgres_test_url)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {key: result[key] for key in ("previous_path", "admitted", "sample_index_increment")}
        )
    )


if __name__ == "__main__":
    main()
