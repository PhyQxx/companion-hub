"""Owned template deletion is atomic while accepted audits and costs are independent."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import delete, update
from test_delivery_sqlite_transactions import explicit_transactions
from test_job_lifecycle_fence import during_commit
from test_operation_authority_fence import prepared
from test_resource_budget import seed

from app.config.models import RunBudgetConfig
from app.contacts import ContactStore
from app.db import (
    ActionPlanRecord,
    ContactRecord,
    ModelCostRecord,
    ModelReservationRecord,
    TaskRunRecord,
    WorkflowDraftRecord,
    WorkflowRecord,
)
from app.ids import uuid7
from app.llm.contracts import ModelPricing, ModelUsage
from app.runs.budget import RunModelBudget
from app.workflows.models import WorkflowStep
from app.workflows.store import WorkflowStore
from scripts.benchmark_storage import FixtureStorage


@asynccontextmanager
async def storage_mode(backend: str, tmp_path: Path) -> AsyncIterator[FixtureStorage]:
    storage = await prepared("postgresql" if backend == "postgresql" else "sqlite", tmp_path)
    try:
        if backend == "sqlite_explicit":
            async with explicit_transactions(storage.database):
                yield storage
        else:
            yield storage
    finally:
        await storage.close()


async def template(
    storage: FixtureStorage, kind: str
) -> tuple[
    UUID,
    UUID,
    UUID,
    Callable[[UUID, UUID], Awaitable[None]],
    type[ContactRecord] | type[WorkflowRecord],
]:
    owner, _, _ = await seed(storage.database)
    other, _, _ = await seed(storage.database)
    if kind == "contact":
        contacts = ContactStore(storage.database)
        view = await contacts.create_contact(user_id=owner, display_name="Synthetic contact")
        return owner, other, view.id, contacts.delete_contact, ContactRecord
    workflows = WorkflowStore(storage.database)
    workflow = await workflows.create_workflow(
        user_id=owner,
        name="Synthetic workflow",
        steps=[WorkflowStep(action_id="test.read", arguments={"value": "synthetic"})],
    )
    return owner, other, workflow.id, workflows.delete_workflow, WorkflowRecord


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
@pytest.mark.parametrize("kind", ["contact", "workflow"])
@pytest.mark.parametrize("change", ["owner", "deleted", "unchanged"])
async def test_delete_rechecks_owned_row_after_writer_commit(
    backend: str,
    kind: str,
    change: str,
    tmp_path: Path,
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        owner, other, entity, remove, model = await template(storage, kind)

        async def follow() -> None:
            if change == "unchanged":
                await remove(owner, entity)
            else:
                with pytest.raises(LookupError, match=f"{kind} not found"):
                    await remove(owner, entity)

        await during_commit(
            storage.database,
            lambda sql: sql.execute(
                delete(model).where(model.id == entity)
                if change == "deleted"
                else update(model)
                .where(model.id == entity)
                .values(user_id=other if change == "owner" else owner)
            ),
            follow,
        )
        async with storage.database.sessions() as sql:
            row = cast(ContactRecord | WorkflowRecord | None, await sql.get(model, entity))
        if change == "owner":
            assert row is not None and row.user_id == other
        else:
            assert row is None


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
@pytest.mark.parametrize("kind", ["contact", "workflow"])
async def test_parallel_delete_returns_one_success_and_one_owned_not_found(
    backend: str,
    kind: str,
    tmp_path: Path,
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        owner, _, entity, remove, model = await template(storage, kind)
        results = await asyncio.gather(
            remove(owner, entity), remove(owner, entity), return_exceptions=True
        )
        assert sum(value is None for value in results) == 1
        assert sum(isinstance(value, LookupError) for value in results) == 1
        async with storage.database.sessions() as sql:
            assert await sql.get(model, entity) is None


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
@pytest.mark.parametrize("kind", ["contact", "workflow"])
async def test_foreign_and_missing_delete_do_not_change_owned_content(
    backend: str,
    kind: str,
    tmp_path: Path,
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        owner, other, entity, remove, model = await template(storage, kind)
        with pytest.raises(LookupError):
            await remove(other, entity)
        with pytest.raises(LookupError):
            await remove(owner, uuid7())
        async with storage.database.sessions() as sql:
            row = cast(ContactRecord | WorkflowRecord, await sql.get_one(model, entity))
        assert row.user_id == owner


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
@pytest.mark.parametrize("kind", ["contact", "workflow"])
async def test_template_delete_retains_accepted_plan_draft_and_financial_audit(
    backend: str,
    kind: str,
    tmp_path: Path,
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        owner, _, entity, remove, model = await template(storage, kind)
        now = datetime.now(UTC)
        plan, draft, run = uuid7(), uuid7(), uuid7()
        async with storage.database.sessions.begin() as sql:
            sql.add(
                TaskRunRecord(
                    id=run,
                    user_id=owner,
                    status="running",
                    privacy_level="L1",
                    contract={"criterion": "synthetic.audit", "required_work": []},
                    budget=RunBudgetConfig().model_dump(mode="json"),
                    deadline=now + timedelta(minutes=5),
                    created_at=now,
                    updated_at=now,
                )
            )
            await sql.flush()
            sql.add(
                ActionPlanRecord(
                    id=plan,
                    user_id=owner,
                    task_run_id=run,
                    title="Independently accepted snapshot",
                    status="completed",
                    idempotency_key=f"synthetic-{plan}",
                    request_hash="synthetic",
                    expires_at=now + timedelta(minutes=5),
                )
            )
            sql.add(
                WorkflowDraftRecord(
                    id=draft,
                    user_id=owner,
                    plan_id=plan,
                    name="Reviewed snapshot",
                    steps=[{"action_id": "test.read", "arguments": {}}],
                    status="approved",
                    replay_status="passed",
                    replay_detail={"workflow_id": str(entity)},
                    dedupe_key=f"{draft.hex}{draft.hex}",
                    created_at=now,
                )
            )
        budget = RunModelBudget(
            storage.database, run_id=run, user_id=owner, config=RunBudgetConfig()
        )
        permit = await budget.reserve(
            endpoint="synthetic",
            tokens=100,
            final=True,
            pricing=ModelPricing(input_rate=2, output_rate=5, currency="CNY"),
        )
        await budget.settle(
            permit.call_id,
            ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15, usage_known=True),
        )
        await remove(owner, entity)
        async with storage.database.sessions() as sql:
            assert await sql.get(model, entity) is None
            saved_plan = await sql.get_one(ActionPlanRecord, plan)
            saved_draft = await sql.get_one(WorkflowDraftRecord, draft)
            receipt = await sql.get_one(ModelReservationRecord, permit.call_id)
            fee = await sql.get_one(ModelCostRecord, permit.call_id)
        assert saved_plan.status == "completed" and saved_plan.task_run_id == run
        assert saved_draft.status == "approved" and saved_draft.plan_id == plan
        assert receipt.run_id == run and receipt.actual_tokens == 15
        assert fee.user_id == owner and fee.state == "estimated" and fee.charged_micros == 45
