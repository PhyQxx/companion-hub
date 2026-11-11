"""Finished maintenance jobs retain the consumer after the lease is cleared."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from test_goal_delivery_runs import database as database

from app.db import Database, JobStepRecord
from app.jobs.maintenance import MaintenanceFailure, MaintenanceSourceGone, MaintenanceWorker


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled"])
async def test_consumer_survives_terminal_lease_cleanup(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    build = tmp_path / "build.json"
    build.write_text(
        '{"revision":"fixture-revision","source_digest":"fixture-digest","secret":"not copied"}'
    )
    monkeypatch.setenv("ARIA_BUILD_INFO_PATH", str(build))

    async def handler(kind: str, payload: dict[str, Any], owner: str) -> None:
        if outcome == "failed":
            raise MaintenanceFailure("fixture_failure", {"stage": "fixture"})
        if outcome == "cancelled":
            raise MaintenanceSourceGone()

    worker = MaintenanceWorker(database, handler, resource_class="fixture-identity")
    job = await worker.engine.submit(
        "fixture", {"secret": "private payload"}, resource_class="fixture-identity", max_attempts=1
    )
    # Identity belongs to the loaded consumer, not a later change to build files.
    build.write_text('{"revision":"different-build"}')
    await worker.process_ready()
    finished = await worker.engine.get(job.id)
    assert finished and finished.status == outcome and finished.lease_owner is None
    async with database.sessions() as session:
        step = (
            await session.scalars(select(JobStepRecord).where(JobStepRecord.job_id == job.id))
        ).one()
        assert step.checkpoint is not None
        consumer = step.checkpoint["consumer"]
        assert consumer["worker_id"] == worker._worker_id
        assert consumer["pid"] > 0
        assert consumer["build"]["revision"] == "fixture-revision"
        assert "secret" not in str(step.checkpoint)
        assert "private payload" not in str(step.checkpoint)
        assert step.attempt == 1


def test_unknown_build_and_distinct_consumers(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARIA_BUILD_INFO_PATH", str(tmp_path / "missing.json"))

    async def handler(kind: str, payload: dict[str, Any], owner: str) -> None:
        pass

    first = MaintenanceWorker(database, handler, resource_class="fixture")
    second = MaintenanceWorker(database, handler, resource_class="fixture")
    assert first._identity["worker_id"] != second._identity["worker_id"]
    assert first._identity["build"]["revision"] is None
    assert first._identity["build"]["version"].endswith("+dev")


async def test_retry_retains_both_consumer_attempts(database: Database) -> None:
    from datetime import UTC, datetime, timedelta

    from app.db import JobRecord

    async def failing(kind: str, payload: dict[str, Any], owner: str) -> None:
        raise MaintenanceFailure("retry_fixture", {"stage": "fixture"})

    async def succeeding(kind: str, payload: dict[str, Any], owner: str) -> None:
        pass

    first = MaintenanceWorker(database, failing, resource_class="fixture-retry")
    second = MaintenanceWorker(database, succeeding, resource_class="fixture-retry")
    job = await first.engine.submit("fixture", {}, resource_class="fixture-retry", max_attempts=2)
    await first.process_ready()
    async with database.sessions.begin() as session:
        row = await session.get_one(JobRecord, job.id)
        assert row.status == "retry_wait"
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
    await second.process_ready()
    finished = await second.engine.get(job.id)
    assert finished and finished.status == "succeeded"
    async with database.sessions() as session:
        steps = list(
            await session.scalars(
                select(JobStepRecord)
                .where(JobStepRecord.job_id == job.id)
                .order_by(JobStepRecord.attempt)
            )
        )
        assert [step.attempt for step in steps] == [1, 2]
        assert [(step.checkpoint or {})["consumer"]["worker_id"] for step in steps] == [
            first._worker_id,
            second._worker_id,
        ]
