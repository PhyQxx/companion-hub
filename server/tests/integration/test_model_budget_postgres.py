"""Exercise atomic admission/settlement on isolated PostgreSQL schemas in CI."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from test_run_budget import make_budget

from app.config.models import RunBudgetConfig
from app.db import Base, JobRecord, TaskRunRecord, create_database
from app.harness.budget import BudgetDenied
from app.ids import uuid7
from app.jobs import JobEngine
from app.llm.contracts import ModelUsage


async def test_postgres_concurrent_budget_admission_and_settlement() -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("integration test requires a dedicated _test database")
    database = create_database(url)
    schema = f"budget_{uuid7().hex}"
    created = False
    try:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
            created = True
        database.engine.update_execution_options(schema_translate_map={None: schema})
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        budget = await make_budget(database, RunBudgetConfig(max_tokens=1024))
        results = await asyncio.gather(
            *(budget.reserve(endpoint="test", tokens=700, final=True) for _ in range(16)),
            return_exceptions=True,
        )
        permits = [result for result in results if not isinstance(result, BaseException)]
        assert len(permits) == 1
        assert all(
            isinstance(result, BudgetDenied)
            for result in results
            if isinstance(result, BaseException)
        )
        await asyncio.gather(
            *(budget.settle(permits[0].call_id, ModelUsage(total_tokens=100)) for _ in range(8))
        )
        async with database.sessions() as session:
            row = await session.scalar(
                select(TaskRunRecord).where(TaskRunRecord.id == budget._run_id)
            )
            assert row is not None and row.llm_attempts == 1 and row.budget_tokens == 100
        engine = JobEngine(database)
        job = await engine.submit("deleg.test", {}, resource_class="deleg")
        claims = await asyncio.gather(
            *(engine.claim(f"worker-{i}", resource_class="deleg") for i in range(16))
        )
        owners = [claim for claim in claims if claim is not None]
        assert len(owners) == 1 and owners[0].attempts == 1
        assert await engine.claim("other", resource_class="deleg") is None
        first = owners[0]
        assert first.lease_owner is not None
        step = await engine.start_step(job.id, "read", worker_id=first.lease_owner, claim_version=1)
        async with database.sessions.begin() as session:
            await session.execute(
                update(JobRecord)
                .where(JobRecord.id == job.id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        claims = await asyncio.gather(
            *(engine.claim("same-worker", resource_class="deleg") for _ in range(16))
        )
        owners = [claim for claim in claims if claim is not None]
        assert len(owners) == 1 and owners[0].attempts == 2
        assert not await engine.complete_step(step, worker_id=first.lease_owner, claim_version=1)
        assert not await engine.succeed(job.id, worker_id=first.lease_owner, claim_version=1)
    finally:
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
