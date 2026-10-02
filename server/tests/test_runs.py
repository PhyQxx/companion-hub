from pathlib import Path

import pytest
from test_chat import _summary_service, create_user

from app.chat import TurnCancelled
from app.ids import uuid7
from app.schemas import PrivacyLevel


async def test_chat_run_has_ordered_events_and_owner_isolation(tmp_path: Path) -> None:
    service, database, _ = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="run")
        result = await service.send_message(
            conversation.id, user_id=user.id, text="hello", privacy_level=PrivacyLevel.L1
        )
        run_id = result.assistant_message.turn_id
        run = await service.runs.get(run_id, user_id=user.id)
        assert run.status == "succeeded"
        events = await service.runs.events(run_id, user_id=user.id)
        assert [event.kind for event in events] == ["run.accepted", "run.running", "run.succeeded"]
        assert [event.seq for event in events] == [1, 2, 3]
        assert len(await service.runs.events(run_id, user_id=user.id, after_seq=2)) == 1
        with pytest.raises(LookupError):
            await service.runs.get(run_id, user_id=uuid7())
        with pytest.raises(LookupError):
            await service.runs.events(run_id, user_id=uuid7())
        assert await service.runs.list_runs(user_id=uuid7()) == []
        assert not await service.cancel_run(run_id, user_id=user.id)
        assert (await service.runs.get(run_id, user_id=user.id)).status == "succeeded"
    finally:
        await service.drain_background_work()
        await database.close()


async def test_cancelled_run_cannot_be_completed_and_delete_removes_events(tmp_path: Path) -> None:
    service, database, requests = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="cancel")
        pending = await service.start_turn(
            conversation.id, user_id=user.id, text="hello", privacy_level=PrivacyLevel.L1
        )
        assert await service.cancel_run(pending.turn_id, user_id=user.id)
        run = await service.runs.get(pending.turn_id, user_id=user.id)
        assert run.status == "cancelled" and run.cancel_epoch == 1

        async def delta(value: str) -> None:
            pass

        with pytest.raises(TurnCancelled):
            await service.run_stream(pending, delta)
        assert requests == []
        await service.delete_conversation(conversation.id, user_id=user.id)
        assert await service.runs.list_runs(user_id=user.id) == []
        with pytest.raises(LookupError):
            await service.runs.events(pending.turn_id, user_id=user.id)
    finally:
        await service.drain_background_work()
        await database.close()


async def test_recovery_cancels_run_without_manufacturing_reply(tmp_path: Path) -> None:
    service, database, requests = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="recovery")
        pending = await service.start_turn(
            conversation.id, user_id=user.id, text="hello", privacy_level=PrivacyLevel.L1
        )
        await service.recover_incomplete_turns()
        run = await service.runs.get(pending.turn_id, user_id=user.id)
        assert run.status == "cancelled"
        assert requests == []
        assert len(await service.list_messages(conversation.id, user_id=user.id)) == 1
    finally:
        await service.drain_background_work()
        await database.close()


@pytest.mark.parametrize("deleted", [False, True])
async def test_postcommit_survives_restart_and_deleted_source_stops_derivation(
    tmp_path: Path,
    deleted: bool,
) -> None:
    from sqlalchemy import select
    from test_chat import FakeRouter

    from app.chat import ChatService
    from app.db import JobRecord
    from app.memory import MemoryIngester, MemoryStore, RuleBasedExtractor

    service, database, requests = await _summary_service(tmp_path)
    memory_store = MemoryStore(database)
    service._memory_store = memory_store
    service._memory_ingester = MemoryIngester(memory_store)
    service._postcommit._stop.set()  # Simulate commit followed by process exit before delivery.
    restarted = ChatService(
        database,
        service._config_store,
        memory_store=memory_store,
        memory_extractor=RuleBasedExtractor(),
        router_builder=lambda config: FakeRouter("test", requests),
    )
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="restart")
        await service.send_message(
            conversation.id, user_id=user.id, text="我不吃香菜。", privacy_level=PrivacyLevel.L1
        )
        async with database.sessions() as session:
            jobs = list(await session.scalars(select(JobRecord)))
            assert len(jobs) == 1 and jobs[0].status == "queued"
            assert "香菜" not in str(jobs[0].input)
        if deleted:
            await service.delete_conversation(conversation.id, user_id=user.id)
        await restarted._postcommit.process_ready()
        first = await memory_store.list_memories(user_id=user.id)
        assert bool(first) is not deleted
        await restarted._postcommit.process_ready()
        assert [memory.id for memory in await memory_store.list_memories(user_id=user.id)] == [
            memory.id for memory in first
        ]
        async with database.sessions() as session:
            job = await session.get(JobRecord, jobs[0].id)
            assert job is not None
            assert job.status == ("cancelled" if deleted else "succeeded")
    finally:
        await service.drain_background_work()
        await restarted.drain_background_work()
        await database.close()


async def test_postcommit_enqueue_failure_rolls_back_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import func, select

    from app.db import JobRecord, MessageRecord
    from app.memory import MemoryIngester, MemoryStore

    service, database, _ = await _summary_service(tmp_path)
    service._memory_ingester = MemoryIngester(MemoryStore(database))

    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected_enqueue_failure")

    monkeypatch.setattr(service._postcommit.engine, "submit_in_session", fail)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="rollback")
        with pytest.raises(RuntimeError, match="injected_enqueue_failure"):
            await service.send_message(
                conversation.id, user_id=user.id, text="hello", privacy_level=PrivacyLevel.L1
            )
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(MessageRecord)
                    .where(MessageRecord.role == "assistant")
                )
                == 0
            )
            assert await session.scalar(select(func.count()).select_from(JobRecord)) == 0
    finally:
        await service.drain_background_work()
        await database.close()


async def test_client_request_id_replays_reply_and_rejects_changed_input(tmp_path: Path) -> None:
    service, database, requests = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="retry")
        first = await service.send_message(
            conversation.id,
            user_id=user.id,
            text="hello",
            privacy_level=PrivacyLevel.L1,
            client_request_id="request-1",
        )
        replay = await service.send_message(
            conversation.id,
            user_id=user.id,
            text="hello",
            privacy_level=PrivacyLevel.L1,
            client_request_id="request-1",
        )
        assert replay.assistant_message.id == first.assistant_message.id
        assert len(requests) == 1
        assert len(await service.list_messages(conversation.id, user_id=user.id)) == 2
        with pytest.raises(ValueError, match="request_idempotency_conflict"):
            await service.send_message(
                conversation.id,
                user_id=user.id,
                text="changed",
                privacy_level=PrivacyLevel.L1,
                client_request_id="request-1",
            )
        assert len(requests) == 1
    finally:
        await service.drain_background_work()
        await database.close()


async def test_client_request_id_does_not_restart_accepted_turn(tmp_path: Path) -> None:
    service, database, requests = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="in flight")
        await service.start_turn(
            conversation.id,
            user_id=user.id,
            text="hello",
            privacy_level=PrivacyLevel.L1,
            client_request_id="request-1",
        )
        with pytest.raises(ValueError, match="request_already_accepted"):
            await service.send_message(
                conversation.id,
                user_id=user.id,
                text="hello",
                privacy_level=PrivacyLevel.L1,
                client_request_id="request-1",
            )
        assert requests == []
        assert len(await service.list_messages(conversation.id, user_id=user.id)) == 1
    finally:
        await service.drain_background_work()
        await database.close()


async def test_run_api_authentication_owner_scope_and_event_cursor(tmp_path: Path) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api.runs import create_runs_router
    from app.auth import AuthService

    service, database, _ = await _summary_service(tmp_path)
    try:
        auth = AuthService(database)
        owner = await auth.setup(display_name="owner", password="correct horse battery staple")
        other = await create_user(database)
        own = await service.create_conversation(user_id=owner.principal.user_id, title="own")
        foreign = await service.create_conversation(user_id=other.id, title="foreign")
        own_turn = await service.send_message(
            own.id, user_id=owner.principal.user_id, text="hello", privacy_level=PrivacyLevel.L1
        )
        foreign_turn = await service.send_message(
            foreign.id, user_id=other.id, text="hello", privacy_level=PrivacyLevel.L1
        )
        app = FastAPI()
        app.include_router(create_runs_router(service, auth))
        headers = {"Authorization": f"Bearer {owner.access_token}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/v1/runs")).status_code == 401
            runs = await client.get("/api/v1/runs", headers=headers)
            assert runs.status_code == 200
            assert [run["id"] for run in runs.json()] == [str(own_turn.assistant_message.turn_id)]
            run_id = own_turn.assistant_message.turn_id
            events = await client.get(f"/api/v1/runs/{run_id}/events?after_seq=2", headers=headers)
            assert events.status_code == 200
            assert [event["seq"] for event in events.json()] == [3]
            other_id = foreign_turn.assistant_message.turn_id
            assert (
                await client.get(f"/api/v1/runs/{other_id}", headers=headers)
            ).status_code == 404
            assert (
                await client.post(f"/api/v1/runs/{other_id}/cancel", headers=headers)
            ).status_code == 404
            assert (
                await client.get(f"/api/v1/runs/{other_id}/events", headers=headers)
            ).status_code == 404
    finally:
        await service.drain_background_work()
        await database.close()


async def test_delete_source_purges_candidates_goals_and_stops_detached_jobs(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.cognition import CognitiveStore
    from app.cognition.models import GoalKind
    from app.db import (
        ActionPlanRecord,
        CognitiveGoalRecord,
        JobRecord,
        SkillDraftRecord,
        WorkflowDraftRecord,
    )
    from app.jobs import JobEngine
    from app.skills.generator import SkillProposal
    from app.skills.models import SkillDocument
    from app.skills.store import SkillStore
    from app.workflows.drafts import WorkflowDraftStore
    from app.workflows.models import WorkflowStep

    service, database, _ = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="source")
        pending = await service.start_turn(
            conversation.id, user_id=user.id, text="测试来源", privacy_level=PrivacyLevel.L1
        )
        skill_store = SkillStore(database)
        draft = await skill_store.save_draft(
            SkillProposal(
                document=SkillDocument(
                    name="source-fixture", description="Synthetic", instructions="Synthetic"
                ),
                warnings=[],
                evidence=[],
            ),
            system_name="example",
            source="chat",
            turn_id=str(pending.turn_id),
        )
        assert draft is not None
        await CognitiveStore(database).create_goal(
            user_id=user.id,
            kind=GoalKind.USER,
            title="测试目标",
            source_kind="message",
            source_id=str(pending.user_message.id),
        )
        plan_id = uuid7()
        now = datetime.now(UTC)
        async with database.sessions.begin() as session:
            session.add(
                ActionPlanRecord(
                    id=plan_id,
                    user_id=user.id,
                    task_run_id=pending.turn_id,
                    status="completed",
                    idempotency_key="source-fixture",
                    request_hash="fixture",
                    expires_at=now + timedelta(minutes=10),
                )
            )
        workflow_store = WorkflowDraftStore(database)
        workflow_draft = await workflow_store.create_draft(
            user_id=user.id,
            plan_id=plan_id,
            name="Synthetic",
            steps=[WorkflowStep(action_id="test.read")],
        )
        assert workflow_draft is not None
        engine = JobEngine(database)
        job = await engine.submit(
            "deleg.test",
            {"user_id": str(user.id), "turn_id": str(pending.turn_id), "topic": "敏感主题"},
            owner=str(user.id),
            source_turn_id=pending.turn_id,
            resource_class="deleg",
        )
        claimed = await engine.claim("worker", resource_class="deleg")
        assert claimed is not None
        await service.delete_conversation(conversation.id, user_id=user.id)
        async with database.sessions() as session:
            assert await session.scalar(select(SkillDraftRecord)) is None
            assert await session.scalar(select(WorkflowDraftRecord)) is None
            assert await session.scalar(select(CognitiveGoalRecord)) is None
            stored = await session.get(JobRecord, job.id)
            assert (
                stored is not None
                and stored.status == "cancelling"
                and stored.input == {"source_deleted": True}
            )
            plan = await session.get(ActionPlanRecord, plan_id)
            assert plan is not None and plan.reason_code == "source_deleted"
        assert not await engine.claim_active(job.id, "worker", claimed.attempts)
        # A writer which captured a plan before deletion cannot recreate a draft.
        assert (
            await workflow_store.create_draft(
                user_id=user.id,
                plan_id=plan_id,
                name="Late",
                steps=[WorkflowStep(action_id="test.other")],
            )
            is None
        )
    finally:
        await service.drain_background_work()
        await database.close()


async def test_completed_reply_tracks_required_work_and_stops_children(tmp_path: Path) -> None:
    from app.jobs import JobEngine

    service, database, _ = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="work")
        result = await service.send_message(
            conversation.id, user_id=user.id, text="hello", privacy_level=PrivacyLevel.L1
        )
        run_id = result.assistant_message.turn_id
        engine = JobEngine(database)
        job = await engine.submit(
            "deleg.test", {"synthetic": True}, owner=str(user.id), source_turn_id=run_id
        )
        run = await service.runs.get(run_id, user_id=user.id)
        assert run.status == "succeeded"
        assert run.goal is not None and run.goal.status == "pending"
        assert run.goal.passed == 1 and run.goal.pending == 1
        events = await service.runs.events(run_id, user_id=user.id)
        assert events[-1].kind == "run.work.required"
        assert events[-1].payload["work_id"] == str(job.id)
        with pytest.raises(LookupError):
            await service.cancel_run(run_id, user_id=uuid7())
        assert await service.cancel_run(run_id, user_id=user.id)
        assert not await service.cancel_run(run_id, user_id=user.id)
        fresh = await engine.get(job.id)
        assert fresh is not None and fresh.status == "cancelled"
        run = await service.runs.get(run_id, user_id=user.id)
        assert run.status == "succeeded" and run.cancel_epoch == 1
        assert run.goal is not None and run.goal.status == "failed"
        with pytest.raises(ValueError, match="task_run_inactive"):
            await engine.submit("deleg.test", {}, owner=str(user.id), source_turn_id=run_id)
        await service.delete_conversation(conversation.id, user_id=user.id)
        with pytest.raises(LookupError, match="task run not found"):
            await engine.submit("deleg.test", {}, owner=str(user.id), source_turn_id=run_id)
    finally:
        await service.drain_background_work()
        await database.close()


@pytest.mark.parametrize(
    ("status", "verification", "policy", "expected", "level"),
    [
        ("completed", "verified", "read_after_write", "passed", "V3"),
        ("completed", "verified", "receipt", "passed", "V1"),
        ("completed", "not_required", "none", "inconclusive", "V0"),
        ("unknown_outcome", "inconclusive", "read_after_write", "inconclusive", "V0"),
        ("ready", "pending", "read_after_write", "pending", "V0"),
        ("failed", "inconclusive", "read_after_write", "failed", "V0"),
    ],
)
def test_goal_uses_evidence_not_handler_completion(
    status: str, verification: str, policy: str, expected: str, level: str
) -> None:
    from app.db import ActionPlanRecord, ActionStepRecord, JobRecord, TaskRunRecord
    from app.runs.goals import goal_view

    plan_id, run_id = uuid7(), uuid7()
    run = TaskRunRecord(
        id=run_id,
        status="succeeded",
        contract={
            "criterion": "reply_committed",
            "required_work": [{"kind": "action_plan", "id": str(plan_id)}],
        },
    )
    plan = ActionPlanRecord(
        id=plan_id,
        status="completed"
        if status in {"completed", "unknown_outcome"}
        else "ready"
        if status == "ready"
        else "failed",
    )
    step = ActionStepRecord(
        id=uuid7(),
        plan_id=plan_id,
        status=status,
        risk="A1",
        verification_status=verification,
        verification_policy=policy,
        verifier_id="home.entity_state",
    )
    # Optional maintenance failure does not become a required business criterion.
    goal = goal_view(
        run, [plan], [step], [JobRecord(id=uuid7(), kind="chat.postcommit", status="failed")]
    )
    assert goal.status == expected and goal.required == 2
    assert goal.criteria[-1].validation_level == level


def test_successful_delegate_is_not_semantic_proof() -> None:
    from app.db import JobRecord, TaskRunRecord
    from app.runs.goals import goal_view

    job_id = uuid7()
    run = TaskRunRecord(
        id=uuid7(),
        status="succeeded",
        contract={"required_work": [{"kind": "delegated_job", "id": str(job_id)}]},
    )
    goal = goal_view(run, [], [], [JobRecord(id=job_id, status="succeeded", kind="deleg.test")])
    assert goal.status == "inconclusive"
    assert goal.criteria[0].reason_code == "delegated_result_unverified"
    assert goal.criteria[0].validation_level == "V0"


async def test_run_stop_withdraws_unstarted_actions_and_fences_active_delegate(
    tmp_path: Path,
) -> None:
    from app.cognition import ActionInvocation, ActionPlanService, build_builtin_action_registry
    from app.jobs import JobEngine

    service, database, _ = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="stop")
        pending = await service.start_turn(
            conversation.id, user_id=user.id, text="hello", privacy_level=PrivacyLevel.L1
        )
        plans = ActionPlanService(database, build_builtin_action_registry())
        plan = await plans.create_plan(
            user_id=user.id,
            source_turn_id=pending.turn_id,
            invocations=[
                ActionInvocation(action_id="home.light.turn_off", arguments={"target": "synthetic"})
            ],
            idempotency_key="synthetic-run-stop",
        )
        engine = JobEngine(database)
        job = await engine.submit(
            "deleg.test",
            {},
            owner=str(user.id),
            source_turn_id=pending.turn_id,
            resource_class="deleg",
        )
        claimed = await engine.claim("synthetic-worker", resource_class="deleg")
        assert claimed is not None
        assert await service.cancel_turn(
            pending.generation_id, user_id=user.id, reason="voice_interrupted"
        )
        fresh = await plans.get_plan(user_id=user.id, plan_id=plan.id)
        assert fresh.status == "cancelled" and fresh.cancel_requested
        assert all(step.status == "cancelled" for step in fresh.steps)
        assert not await engine.claim_active(job.id, "synthetic-worker", claimed.attempts)
        with pytest.raises(ValueError, match="task_run_inactive"):
            await plans.create_plan(
                user_id=user.id,
                source_turn_id=pending.turn_id,
                invocations=[
                    ActionInvocation(action_id="home.light.turn_off", arguments={"target": "late"})
                ],
            )
        events = await service.runs.events(pending.turn_id, user_id=user.id)
        assert sum(event.kind == "run.work.required" for event in events) == 2
    finally:
        await service.drain_background_work()
        await database.close()
