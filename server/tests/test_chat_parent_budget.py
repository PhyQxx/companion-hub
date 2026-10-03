"""Real chat children share their root's quota and cannot outlive its authority."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from test_llm import FakeProvider
from test_resource_budget import Args, SpyTool, seed
from test_run_cancel_fence import prepared
from test_voice_websocket import config_yaml

from app.chat import ChatService, PendingTurn
from app.config import DatabaseConfigStore
from app.config.models import RunBudgetConfig
from app.db import MessageRecord, ModelReservationRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, current_budget
from app.llm import CompletionRequest, CompletionResult, LLMRouter, ToolCall
from app.runs.budget import RunModelBudget
from app.schemas import PrivacyLevel
from app.tools import ToolRegistry
from scripts.benchmark_storage import FixtureStorage


async def fixture(
    backend: str, tmp_path: Path, *, enabled: bool = True, provider: FakeProvider | None = None
) -> tuple[FixtureStorage, ChatService, UUID, UUID, UUID, FakeProvider]:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        path = tmp_path / "chat-parent.yaml"
        path.write_text(config_yaml())
        store = DatabaseConfigStore(storage.database, path)
        await store.load()
        chosen = provider or FakeProvider("cloud")
        service = ChatService(
            storage.database,
            store,
            router_builder=lambda config: LLMRouter(
                endpoints=config.models,
                routes=config.routes,
                providers={"cloud": chosen, "local": FakeProvider("local")},
            ),
        )
        conversation = await service.create_conversation(user_id=owner, title="synthetic parent")
        config = RunBudgetConfig(enabled=enabled, max_llm_attempts=2, max_tool_attempts=1)
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == root)
                .values(conversation_id=conversation.id, budget=config.model_dump(mode="json"))
            )
        return storage, service, owner, root, conversation.id, chosen
    except BaseException:
        await storage.close()
        raise


async def start(service: ChatService, owner: UUID, root: UUID, conversation: UUID) -> PendingTurn:
    return await service.start_turn(
        conversation,
        user_id=owner,
        text="synthetic utterance",
        privacy_level=PrivacyLevel.L1,
        parent_run_id=root,
    )


async def discard_delta(delta: str) -> None:
    pass


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_children_share_quota_and_preserve_root_opt_out(
    backend: str, enabled: bool, tmp_path: Path
) -> None:
    storage, service, owner, root, conversation, provider = await fixture(
        backend, tmp_path, enabled=enabled
    )
    try:
        children = []
        for _ in range(2):
            pending = await start(service, owner, root, conversation)
            budget = service._model_budget(pending)
            assert (budget is not None) is enabled
            if budget is not None:
                assert budget.run_id == root
            children.append(pending.turn_id)
            await service.run_stream(pending, discard_delta)
        pending = await start(service, owner, root, conversation)
        if enabled:
            with pytest.raises(BudgetDenied, match="run_budget_exhausted"):
                await service.run_stream(pending, discard_delta)
        else:
            await service.run_stream(pending, discard_delta)
        async with storage.database.sessions() as session:
            parent = await session.get_one(TaskRunRecord, root)
            assert parent.status == "running" and parent.llm_attempts == (2 if enabled else 0)
            for identifier in children:
                child = await session.get_one(TaskRunRecord, identifier)
                assert child.status == "succeeded" and child.llm_attempts == 0
                assert child.parent_run_id == root and child.contract["budget_parent_id"] == str(
                    root
                )
            reservations = list(await session.scalars(select(ModelReservationRecord)))
            assert all(item.run_id == root for item in reservations)
            assert len(reservations) == (2 if enabled else 0)
        assert len(provider.requests) == (2 if enabled else 3)
    finally:
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "change", ["conversation", "privacy", "expired", "cancelled", "overflow", "budget", "nested"]
)
async def test_invalid_parent_cannot_persist_a_child_or_input(
    backend: str, change: str, tmp_path: Path
) -> None:
    storage, service, owner, root, conversation, provider = await fixture(backend, tmp_path)
    try:
        other = await service.create_conversation(user_id=owner, title="other synthetic")
        async with storage.database.sessions.begin() as session:
            parent = await session.get_one(TaskRunRecord, root)
            if change == "conversation":
                parent.conversation_id = other.id
            elif change == "privacy":
                parent.privacy_level = "L2"
            elif change == "expired":
                parent.deadline = datetime.now(UTC) - timedelta(seconds=1)
            elif change == "cancelled":
                parent.status = "cancelled"
            elif change == "overflow":
                parent.contract = {**parent.contract, "budget_usage_overflow": True}
            elif change == "budget":
                parent.budget = None
            elif change == "nested":
                parent.parent_run_id = root
        with pytest.raises(BudgetDenied):
            await start(service, owner, root, conversation)
        async with storage.database.sessions() as session:
            assert list(await session.scalars(select(TaskRunRecord.id))) == [root]
            assert not list(await session.scalars(select(MessageRecord.id)))
        assert not provider.requests
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_root_cancel_during_stream_stops_child_without_reply(
    backend: str, tmp_path: Path
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingProvider(FakeProvider):
        async def stream(self, request: CompletionRequest, on_delta: object) -> CompletionResult:
            entered.set()
            await release.wait()
            return await self.complete(request)

    storage, service, owner, root, conversation, _ = await fixture(
        backend, tmp_path, provider=WaitingProvider("cloud")
    )
    pending = await start(service, owner, root, conversation)
    task = asyncio.create_task(service.run_stream(pending, discard_delta))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord).where(TaskRunRecord.id == root).values(status="cancelled")
            )
        release.set()
        with pytest.raises(BudgetDenied, match="budget_run_inactive"):
            await asyncio.wait_for(task, 2)
        async with storage.database.sessions() as session:
            child = await session.get_one(TaskRunRecord, pending.turn_id)
            assert child.status == "failed"
            assert not list(
                await session.scalars(
                    select(MessageRecord.id).where(MessageRecord.role == "assistant")
                )
            )
            parent = await session.get_one(TaskRunRecord, root)
            assert parent.llm_attempts == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_tools_use_the_same_root_instead_of_a_fresh_child_allowance(
    backend: str, tmp_path: Path
) -> None:
    storage, service, owner, root, conversation, _ = await fixture(backend, tmp_path)
    tool = SpyTool()
    service._device_tools = ToolRegistry([tool])
    try:
        outcomes = []
        for _ in range(2):
            pending = await start(service, owner, root, conversation)
            pending = replace(pending, tool_names=(tool.name,))
            execution = await service._execute_tool_call(
                pending,
                [
                    ToolCall(
                        id="synthetic",
                        function={
                            "name": tool.name,
                            "arguments": Args(value="fixture").model_dump(),
                        },
                    )
                ],
            )
            outcomes.append(execution[0].result)
        assert outcomes[0].ok and not outcomes[1].ok
        assert outcomes[1].reason_code == "tool_budget_exhausted" and tool.calls == 1
        async with storage.database.sessions() as session:
            parent = await session.get_one(TaskRunRecord, root)
            assert parent.contract["resource_usage"]["tool_attempts"] == 1
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("state", ["running", "succeeded", "cancelled", "deleted"])
async def test_postcommit_restores_parent_quota_and_never_falls_back_to_child(
    backend: str, state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, service, owner, root, conversation, _ = await fixture(backend, tmp_path)
    seen: list[UUID] = []

    async def inspect(pending: PendingTurn, **kwargs: object) -> None:
        budget = current_budget()
        assert isinstance(budget, RunModelBudget)
        seen.append(budget.run_id)
        permit = await budget.reserve(endpoint="synthetic_maintenance", tokens=1, final=False)
        await budget.settle(permit.call_id, None)

    monkeypatch.setattr(service, "_consolidate_memory", inspect)
    try:
        pending = await start(service, owner, root, conversation)
        turn = await service.run_stream(pending, discard_delta)
        async with storage.database.sessions.begin() as session:
            if state == "deleted":
                await session.execute(delete(TaskRunRecord).where(TaskRunRecord.id == root))
            else:
                await session.execute(
                    update(TaskRunRecord).where(TaskRunRecord.id == root).values(status=state)
                )
        call = service._run_postcommit(
            "chat.memory", {"assistant_message_id": str(turn.assistant_message.id)}, str(owner)
        )
        if state == "running":
            # Maintenance cannot consume the final interactive slot of a live root.
            with pytest.raises(BudgetDenied, match="run_budget_exhausted"):
                await call
            assert seen == [root]
        elif state == "succeeded":
            await call
            assert seen == [root]
        else:
            from app.chat.postcommit import PostcommitSourceGone

            with pytest.raises((BudgetDenied, PostcommitSourceGone)):
                await call
            assert not seen
        async with storage.database.sessions() as session:
            child = await session.get_one(TaskRunRecord, pending.turn_id)
            assert child.status == "succeeded" and child.llm_attempts == 0
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_regular_chat_tools_receive_the_recorded_run_budget(
    backend: str, tmp_path: Path
) -> None:
    storage, service, owner, _, conversation, _ = await fixture(backend, tmp_path)
    tool = SpyTool()
    service._device_tools = ToolRegistry([tool])
    try:
        pending = await service.start_turn(
            conversation, user_id=owner, text="synthetic", privacy_level=PrivacyLevel.L1
        )
        pending = replace(
            pending,
            tool_names=(tool.name,),
            config=pending.config.model_copy(
                update={"run_budget": RunBudgetConfig(max_tool_attempts=1)}
            ),
        )
        call = ToolCall(
            id="synthetic", function={"name": tool.name, "arguments": {"value": "fixture"}}
        )
        first = await service._execute_tool_call(pending, [call])
        second = await service._execute_tool_call(pending, [call])
        assert first[0].result.ok and not second[0].result.ok
        assert second[0].result.reason_code == "tool_budget_exhausted" and tool.calls == 1
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_global_opt_out_does_not_remove_an_enabled_parent_budget(
    backend: str, tmp_path: Path
) -> None:
    storage, service, owner, root, conversation, _ = await fixture(backend, tmp_path)
    try:
        assert isinstance(service._config_store, DatabaseConfigStore)
        store = service._config_store
        candidate = store.current.config.model_copy(
            update={"run_budget": RunBudgetConfig(enabled=False)}
        )
        draft = await store.create_draft(candidate, actor="synthetic")
        await store.publish(draft.version, actor="synthetic")
        pending = await start(service, owner, root, conversation)
        assert not pending.config.run_budget.enabled and pending.parent_budget_enabled
        await service.run_stream(pending, discard_delta)
        async with storage.database.sessions() as session:
            assert (await session.get_one(TaskRunRecord, root)).llm_attempts == 1
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_changed_child_link_cannot_use_a_captured_parent_budget(
    backend: str, tmp_path: Path
) -> None:
    storage, service, owner, root, conversation, provider = await fixture(backend, tmp_path)
    try:
        pending = await start(service, owner, root, conversation)
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == pending.turn_id)
                .values(parent_run_id=None)
            )
        with pytest.raises(BudgetDenied, match="chat_parent_changed"):
            await service.run_stream(pending, discard_delta)
        assert not provider.requests
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_parent_cancel_before_commit_rolls_back_reply(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    storage, service, owner, root, conversation, _ = await fixture(backend, tmp_path)
    commit = service._commit_turn

    async def cancel_then_commit(*args: Any, **kwargs: Any) -> Any:
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord).where(TaskRunRecord.id == root).values(status="cancelled")
            )
        return await commit(*args, **kwargs)

    monkeypatch.setattr(service, "_commit_turn", cancel_then_commit)
    try:
        pending = await start(service, owner, root, conversation)
        with pytest.raises(BudgetDenied, match="budget_run_inactive"):
            await service.run_stream(pending, discard_delta)
        async with storage.database.sessions() as session:
            assert not list(
                await session.scalars(
                    select(MessageRecord.id).where(MessageRecord.role == "assistant")
                )
            )
            assert (await session.get_one(TaskRunRecord, pending.turn_id)).status == "failed"
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_cancelled_root_stops_waiting_tool_and_joins_cleanup(
    backend: str, tmp_path: Path
) -> None:
    from pydantic import BaseModel

    from app.tools import ToolContext, ToolResult

    started, closed = asyncio.Event(), asyncio.Event()

    class WaitingTool(SpyTool):
        async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
            self.calls += 1
            started.set()
            try:
                await asyncio.Event().wait()
                return ToolResult(ok=True, tool_name=self.name, latency_ms=0)
            finally:
                await asyncio.sleep(0)
                closed.set()

    storage, service, owner, root, conversation, _ = await fixture(backend, tmp_path)
    tool = WaitingTool()
    service._device_tools = ToolRegistry([tool])
    pending = replace(await start(service, owner, root, conversation), tool_names=(tool.name,))
    call = ToolCall(id="synthetic", function={"name": tool.name, "arguments": {"value": "fixture"}})
    task = asyncio.create_task(service._execute_tool_call(pending, [call]))
    try:
        await asyncio.wait_for(started.wait(), 2)
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord).where(TaskRunRecord.id == root).values(status="cancelled")
            )
        with pytest.raises(BudgetDenied, match="budget_run_inactive"):
            await asyncio.wait_for(task, 2)
        assert closed.is_set() and tool.calls == 1
        async with storage.database.sessions() as session:
            parent = await session.get_one(TaskRunRecord, root)
            assert parent.contract["resource_usage"]["unknown_tool_calls"] == 1
            assert parent.contract["resource_usage"]["tool_attempts"] == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("kind", ["model", "tool"])
@pytest.mark.parametrize("missing", [False, True])
async def test_maintenance_active_root_cannot_bypass_root_deadline(
    backend: str, kind: str, missing: bool, tmp_path: Path
) -> None:
    from app.runs.resources import RunToolBudget

    storage, _, owner, root, _, _ = await fixture(backend, tmp_path)
    try:
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == root)
                .values(deadline=None if missing else datetime.now(UTC) - timedelta(seconds=1))
            )
        config = RunBudgetConfig()
        with pytest.raises(BudgetDenied, match="run_deadline_exceeded"):
            if kind == "model":
                budget = RunModelBudget(
                    storage.database,
                    run_id=root,
                    user_id=owner,
                    config=config,
                    phase="maintenance",
                    allow_active_parent=True,
                )
                await budget.reserve(endpoint="synthetic", tokens=1, final=False)
            else:
                tool_budget = RunToolBudget(
                    storage.database, run_id=root, user_id=owner, config=config, maintenance=True
                )
                await tool_budget.reserve_tool(tool_name="synthetic", user_id=owner)
        async with storage.database.sessions() as session:
            row = await session.get_one(TaskRunRecord, root)
            assert row.llm_attempts == 0 and "resource_usage" not in row.contract
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("kind", ["model", "tool"])
@pytest.mark.parametrize("terminal", [False, True])
async def test_maintenance_permit_uses_live_root_deadline_or_terminal_grace(
    backend: str, kind: str, terminal: bool, tmp_path: Path
) -> None:
    from app.runs.resources import RunToolBudget

    storage, _, owner, root, _, _ = await fixture(backend, tmp_path)
    try:
        now = datetime.now(UTC)
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == root)
                .values(
                    deadline=now + timedelta(seconds=-1 if terminal else 1),
                    status="succeeded" if terminal else "running",
                )
            )
        config = RunBudgetConfig(maintenance_deadline_seconds=10)
        if kind == "model":
            budget = RunModelBudget(
                storage.database,
                run_id=root,
                user_id=owner,
                config=config,
                phase="maintenance",
                allow_active_parent=True,
            )
            permit = await budget.reserve(endpoint="synthetic", tokens=1, final=False)
            seconds = permit.remaining_seconds
            await budget.settle(permit.call_id, None)
        else:
            tool_budget = RunToolBudget(
                storage.database, run_id=root, user_id=owner, config=config, maintenance=True
            )
            tool_permit = await tool_budget.reserve_tool(tool_name="synthetic", user_id=owner)
            seconds = tool_permit.remaining_seconds
            await tool_budget.settle_tool(tool_permit.call_id, reported_ok=None)
        assert 0 < seconds <= (10 if terminal else 1)
        if terminal:
            assert seconds > 5
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("fails", [False, True])
async def test_parent_source_read_finishes_sql_cleanup_before_repeated_cancellation_returns(
    backend: str, fails: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from sqlalchemy.ext.asyncio import AsyncSession

    storage, service, owner, root, conversation, _ = await fixture(backend, tmp_path)
    pending = await start(service, owner, root, conversation)
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    scalar, close = AsyncSession.scalar, AsyncSession.close
    read_was_cancelled = False

    async def wait_after_read(session: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        nonlocal read_was_cancelled
        result = await scalar(session, *args, **kwargs)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            read_was_cancelled = True
            raise
        if fails:
            raise RuntimeError("synthetic_read_failure")
        return result

    async def record_close(session: AsyncSession) -> None:
        await close(session)
        closed.set()

    monkeypatch.setattr(AsyncSession, "scalar", wait_after_read)
    monkeypatch.setattr(AsyncSession, "close", record_close)
    task = asyncio.create_task(service._validate_context(pending))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert closed.is_set() and not read_was_cancelled
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        monkeypatch.undo()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_deleted_chat_run_does_not_restore_unbudgeted_postcommit_work(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.chat.postcommit import PostcommitSourceGone

    storage, service, owner, root, conversation, _ = await fixture(backend, tmp_path)
    called = False

    async def inspect(pending: PendingTurn, **kwargs: object) -> None:
        nonlocal called
        called = True
        assert current_budget() is not None

    monkeypatch.setattr(service, "_consolidate_memory", inspect)
    try:
        pending = await start(service, owner, root, conversation)
        turn = await service.run_stream(pending, discard_delta)
        async with storage.database.sessions.begin() as session:
            await session.execute(delete(TaskRunRecord).where(TaskRunRecord.id == pending.turn_id))
        with pytest.raises(PostcommitSourceGone):
            await service._run_postcommit(
                "chat.memory", {"assistant_message_id": str(turn.assistant_message.id)}, str(owner)
            )
        assert not called
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_deleted_chat_job_source_cannot_become_an_independent_model_root(
    backend: str, tmp_path: Path
) -> None:
    from app.db import JobRecord
    from app.ids import uuid7
    from app.runs.budget import job_model_budget

    storage, service, owner, root, conversation, _ = await fixture(backend, tmp_path)
    identifier = uuid7()
    try:
        pending = await start(service, owner, root, conversation)
        turn = await service.run_stream(pending, discard_delta)
        async with storage.database.sessions.begin() as session:
            session.add(
                JobRecord(
                    id=identifier,
                    task_run_id=pending.turn_id,
                    kind="chat.memory",
                    owner=str(owner),
                    status="running",
                    input={
                        "user_id": str(owner),
                        "assistant_message_id": str(turn.assistant_message.id),
                    },
                )
            )
            await session.flush()
            await session.execute(delete(TaskRunRecord).where(TaskRunRecord.id == pending.turn_id))
        with pytest.raises(BudgetDenied, match="budget_run_not_found"):
            await job_model_budget(storage.database, identifier, RunBudgetConfig())
        async with storage.database.sessions() as session:
            assert await session.get(TaskRunRecord, identifier) is None
    finally:
        await storage.close()
