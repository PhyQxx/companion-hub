"""Exercise atomic admission/settlement on isolated PostgreSQL schemas in CI."""

import asyncio
import os

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from test_run_budget import make_budget

from app.config.models import RunBudgetConfig
from app.db import Base, TaskRunRecord, create_database
from app.harness.budget import BudgetDenied
from app.ids import uuid7
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
    finally:
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
