"""A run's required-work ledger cannot admit stale or lose concurrent children."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import select, update
from test_cognitive_save_guard import database as save_database
from test_delivery_sqlite_transactions import explicit_transactions
from test_job_lifecycle_fence import during_commit
from test_perception import user_id as perception_user
from test_run_transaction_fence import record

from app.cognition import ActionInvocation, ActionPlanService, build_builtin_action_registry
from app.db import ConversationRecord, Database, JobRecord, TaskRunRecord
from app.ids import uuid7
from app.jobs.engine import JobEngine
from app.runs.contracts import lock_source_run

database = save_database
user_id = perception_user


async def source(database: Database, owner: UUID, *, chat: bool) -> UUID:
    row = record(owner)
    async with database.sessions.begin() as session:
        if chat:
            conversation = ConversationRecord(id=uuid7(), user_id=owner, status="active")
            session.add(conversation)
            await session.flush()
            row.conversation_id = conversation.id
        session.add(row)
    return row.id


@pytest.mark.parametrize("chat", [False, True])
async def test_waiting_enrollment_rechecks_committed_run_cancellation(
    database: Database, user_id: UUID, chat: bool
) -> None:
    run_id = await source(database, user_id, chat=chat)
    engine = JobEngine(database)

    async def follow() -> None:
        with pytest.raises(ValueError, match="task_run_inactive"):
            await engine.submit("deleg.fixture", {}, owner=str(user_id), source_turn_id=run_id)

    await during_commit(
        database,
        lambda session: session.execute(
            update(TaskRunRecord).where(TaskRunRecord.id == run_id).values(status="cancelled")
        ),
        follow,
    )
    async with database.sessions() as session:
        assert not list(
            await session.scalars(select(JobRecord).where(JobRecord.task_run_id == run_id))
        )
        assert "required_work" not in (await session.get_one(TaskRunRecord, run_id)).contract


@pytest.mark.parametrize("chat", [False, True])
async def test_parallel_enrollment_keeps_every_required_child(
    database: Database, user_id: UUID, chat: bool
) -> None:
    run_id = await source(database, user_id, chat=chat)
    engine = JobEngine(database)
    jobs = await asyncio.gather(
        *(
            engine.submit(
                "deleg.fixture", {"index": index}, owner=str(user_id), source_turn_id=run_id
            )
            for index in range(12)
        )
    )
    async with database.sessions() as session:
        run = await session.get_one(TaskRunRecord, run_id)
        work = run.contract["required_work"]
        assert {value["id"] for value in work} == {str(job.id) for job in jobs}
        assert len(work) == 12
        assert (
            len(
                list(
                    await session.scalars(select(JobRecord).where(JobRecord.task_run_id == run_id))
                )
            )
            == 12
        )


@pytest.mark.parametrize("chat", [False, True])
async def test_enrollment_fence_blocks_run_revocation_until_derived_commit(
    database: Database, user_id: UUID, chat: bool
) -> None:
    run_id = await source(database, user_id, chat=chat)
    entered, release = asyncio.Event(), asyncio.Event()

    async def enroll() -> None:
        async with database.sessions.begin() as session:
            await lock_source_run(session, run_id, user_id=user_id)
            entered.set()
            await release.wait()
            await JobEngine(database).submit_in_session(
                session, "deleg.fixture", {}, owner=str(user_id), source_turn_id=run_id
            )

    async def revoke() -> None:
        async with database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord).where(TaskRunRecord.id == run_id).values(status="cancelled")
            )

    task = asyncio.create_task(enroll())
    cancellation: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        cancellation = asyncio.create_task(revoke())
        await asyncio.sleep(0.1)
        assert not cancellation.done(), "run revocation passed the work enrollment fence"
        release.set()
        await asyncio.wait_for(task, 3)
        await asyncio.wait_for(cancellation, 3)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if cancellation is not None:
            if not cancellation.done():
                cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)


@pytest.mark.parametrize("kind", ["job", "plan"])
async def test_source_enrollment_with_explicit_sqlite_transactions(
    database: Database, user_id: UUID, kind: str
) -> None:
    run_id = await source(database, user_id, chat=True)
    async with explicit_transactions(database):
        if kind == "job":
            engine = JobEngine(database)
            jobs = await asyncio.gather(
                *(
                    engine.submit(
                        "deleg.fixture",
                        {"index": index},
                        owner=str(user_id),
                        source_turn_id=run_id,
                        idempotency_key=f"fixture-enroll-{index}",
                    )
                    for index in range(8)
                )
            )
            expected = {str(job.id) for job in jobs}
        else:
            service = ActionPlanService(database, build_builtin_action_registry())
            plans = await asyncio.gather(
                *(
                    service.create_plan(
                        user_id=user_id,
                        source_turn_id=run_id,
                        idempotency_key=f"fixture-plan-{index}",
                        invocations=[
                            ActionInvocation(
                                action_id="home.light.turn_off", arguments={"target": "fixture"}
                            )
                        ],
                    )
                    for index in range(8)
                )
            )
            expected = {str(plan.id) for plan in plans}
    async with database.sessions() as session:
        run = await session.get_one(TaskRunRecord, run_id)
        assert {value["id"] for value in run.contract["required_work"]} == expected


async def test_owned_cached_enrollment_cannot_accept_changed_contract(
    database: Database, user_id: UUID
) -> None:
    run_id = await source(database, user_id, chat=False)
    async with database.sessions() as session:
        row = await session.get_one(TaskRunRecord, run_id)
        await session.commit()
        async with database.sessions.begin() as writer:
            await writer.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == run_id)
                .values(contract={"work_cancel_requested": True})
            )
        assert not row.contract
        async with session.begin():
            with pytest.raises(ValueError, match="task_run_inactive"):
                await lock_source_run(session, run_id, user_id=user_id)


async def test_existing_job_replay_after_source_cancellation_does_not_enroll_again(
    database: Database, user_id: UUID
) -> None:
    run_id = await source(database, user_id, chat=True)
    engine = JobEngine(database)
    original = await engine.submit(
        "deleg.fixture",
        {"index": 1},
        owner=str(user_id),
        source_turn_id=run_id,
        idempotency_key="fixture-stable-replay",
    )
    async with database.sessions.begin() as session:
        await session.execute(
            update(TaskRunRecord).where(TaskRunRecord.id == run_id).values(status="cancelled")
        )
    replay = await engine.submit(
        "deleg.fixture",
        {"index": 1},
        owner=str(user_id),
        source_turn_id=run_id,
        idempotency_key="fixture-stable-replay",
    )
    assert replay.id == original.id
    async with database.sessions() as session:
        row = await session.get_one(TaskRunRecord, run_id)
        assert row.status == "cancelled" and len(row.contract["required_work"]) == 1


async def test_parallel_replays_add_one_required_job(database: Database, user_id: UUID) -> None:
    run_id = await source(database, user_id, chat=True)
    engine = JobEngine(database)
    jobs = await asyncio.gather(
        *(
            engine.submit(
                "deleg.fixture",
                {},
                owner=str(user_id),
                source_turn_id=run_id,
                idempotency_key="fixture-parallel-replay",
            )
            for _ in range(12)
        )
    )
    assert len({value.id for value in jobs}) == 1
    async with database.sessions() as session:
        row = await session.get_one(TaskRunRecord, run_id)
        assert row.contract["required_work"] == [{"kind": "delegated_job", "id": str(jobs[0].id)}]


async def test_parallel_plans_keep_each_required_plan(database: Database, user_id: UUID) -> None:
    run_id = await source(database, user_id, chat=True)
    service = ActionPlanService(database, build_builtin_action_registry())
    plans = await asyncio.gather(
        *(
            service.create_plan(
                user_id=user_id,
                source_turn_id=run_id,
                idempotency_key=f"fixture-parallel-plan-{index}",
                invocations=[
                    ActionInvocation(
                        action_id="home.light.turn_off", arguments={"target": "fixture"}
                    )
                ],
            )
            for index in range(12)
        )
    )
    async with database.sessions() as session:
        row = await session.get_one(TaskRunRecord, run_id)
        work = row.contract["required_work"]
        assert {value["id"] for value in work} == {str(plan.id) for plan in plans}
        assert len(work) == 12


@pytest.mark.parametrize(
    "criterion",
    ["model_result_returned", "delivery_channels_returned", "provider_response_returned"],
)
async def test_transport_only_runs_cannot_enroll_business_work(
    database: Database, user_id: UUID, criterion: str
) -> None:
    run_id = await source(database, user_id, chat=False)
    async with database.sessions.begin() as session:
        await session.execute(
            update(TaskRunRecord)
            .where(TaskRunRecord.id == run_id)
            .values(contract={"criterion": criterion})
        )
    with pytest.raises(ValueError, match="_only_run_cannot_enroll_work"):
        await JobEngine(database).submit(
            "deleg.fixture", {}, owner=str(user_id), source_turn_id=run_id
        )
