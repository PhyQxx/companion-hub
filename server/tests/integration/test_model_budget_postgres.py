"""Exercise atomic admission/settlement on isolated PostgreSQL schemas in CI."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from test_run_budget import make_budget

from app.config.models import RunBudgetConfig
from app.db import Base, JobRecord, TaskRunRecord, create_database
from app.harness.budget import BudgetDenied, CallPermit
from app.ids import uuid7
from app.jobs import JobEngine
from app.llm.contracts import ModelUsage


async def test_postgres_concurrent_budget_admission_and_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        # Tool counters and append-only settlements share the same root lock.
        tool_model = await make_budget(database, RunBudgetConfig(max_tool_attempts=1))
        tool_port = tool_model.tool_budget
        tool_results = await asyncio.gather(
            *(
                tool_port.reserve_tool(tool_name="fixture_tool", user_id=tool_model._user_id)
                for _ in range(16)
            ),
            return_exceptions=True,
        )
        from app.harness.budget import ToolPermit
        from app.runs.store import RunStore

        tool_permits = [value for value in tool_results if isinstance(value, ToolPermit)]
        assert len(tool_permits) == 1
        assert all(
            isinstance(value, BudgetDenied) for value in tool_results if value not in tool_permits
        )
        await asyncio.gather(
            *(tool_port.settle_tool(tool_permits[0].call_id, reported_ok=True) for _ in range(8))
        )
        tool_view = await RunStore(database).get(tool_model._run_id, user_id=tool_model._user_id)
        assert tool_view.budget_summary is not None
        assert tool_view.budget_summary.tool_attempts == 1
        assert tool_view.budget_summary.unsettled_tool_calls == 0
        from app.llm.contracts import ModelPricing
        from app.runs.costs import cost_summary

        fee_config = RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.001)
        fee_budget = await make_budget(database, fee_config)
        fee_results = await asyncio.gather(
            *(
                fee_budget.reserve(
                    endpoint="fixture",
                    tokens=700,
                    final=True,
                    pricing=ModelPricing(input_rate=1, output_rate=1, currency="CNY"),
                )
                for _ in range(16)
            ),
            return_exceptions=True,
        )
        fee_permits = [value for value in fee_results if isinstance(value, CallPermit)]
        assert len(fee_permits) == 1
        await asyncio.gather(
            *(
                fee_budget.settle(
                    fee_permits[0].call_id,
                    ModelUsage(
                        input_tokens=20, output_tokens=10, total_tokens=30, usage_known=True
                    ),
                )
                for _ in range(8)
            )
        )
        fee_summary = await cost_summary(database, user_id=fee_budget._user_id)
        assert fee_summary.currencies[0].charged_micros == "30"
        assert fee_summary.currencies[0].estimated_calls == 1
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
        # Hold a valid claim while cancelling its standalone run. The opposite
        # Run -> Job lock order deadlocks admission's Job -> Run transaction.
        from app.db import AppUserRecord
        from app.db.claims import assert_current_claim as original_guard
        from app.harness.claim import ExecutionClaim, claim_scope
        from app.runs.budget import job_model_budget

        user_id = uuid7()
        async with database.sessions.begin() as session:
            session.add(AppUserRecord(id=user_id, display_name="Fence test", status="active"))
        queued = await engine.submit(
            "deleg.cancel-test",
            {"user_id": str(user_id)},
            owner=str(user_id),
            resource_class="cancel-test",
        )
        claimed = await engine.claim("fenced-worker", resource_class="cancel-test")
        assert claimed is not None
        model_budget = await job_model_budget(database, queued.id, RunBudgetConfig())
        assert model_budget is not None
        locked, release = asyncio.Event(), asyncio.Event()

        async def paused_guard(session: object) -> None:
            await original_guard(session)  # type: ignore[arg-type]
            locked.set()
            await release.wait()

        monkeypatch.setattr("app.runs.budget.assert_current_claim", paused_guard)
        with claim_scope(ExecutionClaim(queued.id, "fenced-worker", claimed.attempts)):
            admission = asyncio.create_task(
                model_budget.reserve(endpoint="test", tokens=10, final=True)
            )
        await asyncio.wait_for(locked.wait(), 3)
        cancellation = asyncio.create_task(
            RunStore(database).cancel_background(queued.id, user_id=user_id)
        )
        try:

            async def wait_for_blocked_job_lock() -> None:
                while True:
                    async with database.sessions() as session:
                        blocked = await session.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE wait_event_type = 'Lock' "
                                "AND query LIKE :pattern"
                            ),
                            {"pattern": f"%{schema}%job%"},
                        )
                    if blocked:
                        return
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_blocked_job_lock(), 3)
        finally:
            release.set()
        cancellation_results = await asyncio.wait_for(asyncio.gather(admission, cancellation), 3)
        assert cancellation_results[1] is True
    finally:
        if created:
            async with database.engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        await database.close()
