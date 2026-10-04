"""Browser proactive text and optional audio share an owned delivery scope."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select
from test_voice_budget_terminal import AudioStream, Synthesizer
from test_voice_turn_delivery import fixture
from test_voice_websocket import FakeRecognizer, RecordingWebSocket

from app.db import ModelCostRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, budget_scope
from app.ids import uuid7
from app.runs.budget import RunModelBudget
from app.runs.goals import goal_view
from app.runs.resources import resource_usage
from app.runs.speech_delivery import SqlSpeechDelivery, _Delivery, recover_expired_speech_deliveries
from app.runtime.turn_coordinator import TurnCoordinator
from app.schemas import PrivacyLevel
from app.voice.delivery_ports import VoicePricingSource
from app.voice.factory import StaticVoiceSource
from app.voice.failover import TtsProviderChain


class EndSocket(RecordingWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def send_text(self, value: str) -> None:
        if json.loads(value)["type"] == "voice.sentence.end":
            self.entered.set()
            await self.release.wait()
        await super().send_text(value)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_proactive_root_lives_through_end_frame_and_retains_unknown_cost(
    backend: str, enabled: bool, tmp_path: Path
) -> None:
    socket = EndSocket()
    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), enabled=enabled, socket=socket
    )
    task = asyncio.create_task(
        manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
    )
    try:
        await asyncio.wait_for(socket.entered.wait(), 3)
        async with storage.database.sessions() as sql:
            rows = list(await sql.scalars(select(TaskRunRecord)))
            root = next(
                (row for row in rows if row.contract.get("entry") == "voice.proactive_output"), None
            )
            assert root is not None and root.status == "running"
            child = next(row for row in rows if row.parent_run_id == root.id)
            assert child.status == "succeeded"
            cost = await sql.get_one(ModelCostRecord, child.id)
            assert cost.state == "unknown" and cost.charged_micros == 2000
            assert cost.unit_quantity is None
            assert "synthetic speech" not in str(root.contract) + str(child.contract)
        socket.release.set()
        assert await asyncio.wait_for(task, 3)
        async with storage.database.sessions() as sql:
            closed = await sql.get_one(TaskRunRecord, root.id)
            assert closed.status == "succeeded"
            assert closed.contract["delivery_result"] == "text_with_optional_audio_sent"
            assert goal_view(closed, [], [], []).status == "passed"
        assert provider.calls == provider.stream.closed == 1
        assert socket.texts[-1]["type"] == "voice.sentence.end"
    finally:
        socket.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_proactive_audio_lease_lives_until_end_frame_returns(
    backend: str, tmp_path: Path
) -> None:
    socket = EndSocket()
    storage, manager, session, service, _, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), socket=socket
    )
    coordinator = TurnCoordinator(storage.database, service)
    manager._turns = coordinator
    task = asyncio.create_task(
        manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
    )
    try:
        await asyncio.wait_for(socket.entered.wait(), 3)
        assert await coordinator.current_audio_holder() == session.device_id
        assert session.proactive_audio_lease
        socket.release.set()
        assert await asyncio.wait_for(task, 3)
        assert await coordinator.current_audio_holder() is None
        assert not session.proactive_audio_lease
    finally:
        socket.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("notify", [False, True])
async def test_proactive_audio_preemption_closes_old_scope_and_keeps_new_holder(
    backend: str, notify: bool, tmp_path: Path
) -> None:
    socket = AudioSocket()
    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), socket=socket
    )
    coordinator = TurnCoordinator(storage.database, service)
    manager._turns = coordinator
    manager._sessions[id(session)] = session
    provider.stream = WaitingAudio()
    new_holder = uuid7()
    task = asyncio.create_task(
        manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
    )
    try:
        await asyncio.wait_for(socket.audio_sent.wait(), 3)
        lease = await coordinator.acquire_audio_lease(new_holder, uuid7(), ttl_seconds=120)
        assert lease.previous_holder == session.device_id
        if notify:
            await manager._preempt_audio_holder(session.device_id, exclude=None)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            with pytest.raises(BudgetDenied, match="voice_audio_preempted"):
                await asyncio.wait_for(task, 3)
        assert await coordinator.current_audio_holder() == new_holder
        assert not session.proactive_audio_lease and session.proactive_task is None
        assert provider.stream.closed == 1 and len(socket.audio) == 1
        assert not any(frame["type"] == "voice.sentence.end" for frame in socket.texts)
        async with storage.database.sessions() as sql:
            root = await sql.scalar(
                select(TaskRunRecord).where(TaskRunRecord.parent_run_id.is_(None))
            )
            assert root is not None and root.status in {"failed", "cancelled"}
            fee = await sql.scalar(select(ModelCostRecord))
            assert fee is not None and fee.charged_micros == 2000
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await coordinator.release_audio_lease(new_holder)
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_cancelled_proactive_lease_acquisition_joins_sql_and_releases_late_lease(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import UUID

    from app.runtime.lease import LeaseResult

    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused")
    )
    coordinator = TurnCoordinator(storage.database, service)
    manager._turns = coordinator
    entered, release = asyncio.Event(), asyncio.Event()
    original = coordinator.acquire_audio_lease

    async def acquire(
        device_id: UUID, generation_id: UUID, *, ttl_seconds: float = 30
    ) -> LeaseResult:
        lease = await original(device_id, generation_id, ttl_seconds=ttl_seconds)
        entered.set()
        await release.wait()
        return lease

    monkeypatch.setattr(coordinator, "acquire_audio_lease", acquire)
    task = asyncio.create_task(
        manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done()
        assert await coordinator.current_audio_holder() == session.device_id
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert await coordinator.current_audio_holder() is None
        assert not session.proactive_audio_lease and provider.stream.closed == 1
        async with storage.database.sessions() as sql:
            root = await sql.scalar(
                select(TaskRunRecord).where(TaskRunRecord.parent_run_id.is_(None))
            )
            assert root is not None and root.status == "cancelled"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_proactive_reuses_original_parent_tool_allowance_without_reset(
    backend: str, tmp_path: Path
) -> None:
    storage, manager, session, service, provider, _, config = await fixture(
        backend, tmp_path, FakeRecognizer("unused")
    )
    parent_id = uuid7()
    parent_config = config.current.config.run_budget.model_copy(update={"max_tool_attempts": 1})
    now = datetime.now(UTC)
    try:
        async with storage.database.sessions.begin() as sql:
            sql.add(
                TaskRunRecord(
                    id=parent_id,
                    user_id=session.principal.user_id,
                    conversation_id=session.conversation_id,
                    request_id=f"synthetic:{parent_id}",
                    status="running",
                    privacy_level="L1",
                    config_version=config.current.version,
                    budget=parent_config.model_dump(mode="json"),
                    deadline=now + timedelta(seconds=90),
                    created_at=now,
                    updated_at=now,
                    contract={"entry": "synthetic.proactive_parent", "required_work": []},
                )
            )
        with budget_scope(
            RunModelBudget(
                storage.database,
                run_id=parent_id,
                user_id=session.principal.user_id,
                config=parent_config,
            )
        ):
            assert await manager._send_proactive(session, "first synthetic speech", PrivacyLevel.L1)
            provider.stream = AudioStream(None)
            with pytest.raises(BudgetDenied, match="tool_budget_exhausted"):
                await manager._send_proactive(session, "second synthetic speech", PrivacyLevel.L1)
        assert provider.calls == 1
        async with storage.database.sessions() as sql:
            parent = await sql.get_one(TaskRunRecord, parent_id)
            assert parent.status == "running" and resource_usage(parent)["tool_attempts"] == 1
            roots = list(
                await sql.scalars(
                    select(TaskRunRecord).where(TaskRunRecord.parent_run_id == parent_id)
                )
            )
            assert len(roots) == 2 and {root.status for root in roots} == {"failed", "succeeded"}
            assert all(root.deadline == parent.deadline for root in roots)
            costs = list(await sql.scalars(select(ModelCostRecord)))
            assert len(costs) == 1 and costs[0].charged_micros == 2000
    finally:
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_parallel_proactive_attempt_cannot_create_another_scope_on_busy_session(
    backend: str, tmp_path: Path
) -> None:
    socket = AudioSocket()
    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), socket=socket
    )
    provider.stream = WaitingAudio()
    task = asyncio.create_task(
        manager._send_proactive(session, "first synthetic speech", PrivacyLevel.L1)
    )
    try:
        await asyncio.wait_for(socket.audio_sent.wait(), 3)
        assert not await manager._send_proactive(
            session, "second synthetic speech", PrivacyLevel.L1
        )
        assert provider.calls == 1
        async with storage.database.sessions() as sql:
            roots = list(
                await sql.scalars(
                    select(TaskRunRecord).where(TaskRunRecord.parent_run_id.is_(None))
                )
            )
            assert len(roots) == 1 and roots[0].status == "running"
            assert len(list(await sql.scalars(select(ModelCostRecord)))) == 1
        await manager.disconnect(session)
        assert task.cancelled()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_proactive_missing_quote_does_not_dispatch_under_money_cap(
    backend: str, tmp_path: Path
) -> None:
    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), priced=False
    )
    try:
        with pytest.raises(BudgetDenied, match="voice_cost_estimate_unavailable"):
            await manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
        assert provider.calls == 0
        async with storage.database.sessions() as sql:
            root = await sql.scalar(select(TaskRunRecord))
            assert root is not None and root.status == "failed"
            assert not list(await sql.scalars(select(ModelCostRecord)))
    finally:
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_proactive_partial_audio_keeps_text_count_but_fails_delivery(
    backend: str, tmp_path: Path
) -> None:
    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused")
    )
    provider.stream = AudioStream(RuntimeError("synthetic"), late=True)
    try:
        assert await manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
        async with storage.database.sessions() as sql:
            root = await sql.scalar(
                select(TaskRunRecord).where(TaskRunRecord.parent_run_id.is_(None))
            )
            assert root is not None and root.status == "failed"
            assert goal_view(root, [], [], []).status == "failed"
            cost = await sql.scalar(select(ModelCostRecord))
            assert cost is not None and cost.state == "unknown" and cost.charged_micros == 2000
        socket = cast(RecordingWebSocket, session.websocket)
        assert len(socket.audio) == 1 and socket.texts[0]["type"] == "proactive.committed"
        assert not any(frame["type"] == "voice.sentence.end" for frame in socket.texts)
    finally:
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_proactive_uses_captured_session_privacy_for_provider_selection(
    backend: str, tmp_path: Path
) -> None:
    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused")
    )
    session.privacy_level = PrivacyLevel.L2
    provider.runs_local = False
    try:
        assert await manager._send_proactive(session, "public synthetic speech", PrivacyLevel.L0)
        assert provider.calls == 0
        socket = cast(RecordingWebSocket, session.websocket)
        assert socket.texts[-1]["type"] == "voice.tts_unavailable" and not socket.audio
        async with storage.database.sessions() as sql:
            root = await sql.scalar(select(TaskRunRecord))
            assert root is not None and root.status == "succeeded" and root.privacy_level == "L2"
            assert not list(await sql.scalars(select(ModelCostRecord)))
    finally:
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_proactive_before_first_audio_fallback_retains_both_attempt_costs(
    backend: str, tmp_path: Path
) -> None:
    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused")
    )
    provider.stream = AudioStream(RuntimeError("synthetic"))
    backup = Synthesizer(AudioStream(None))
    old_source = cast(VoicePricingSource, manager._voice_source)
    binding = old_source.binding_for(provider)
    assert binding is not None
    source = StaticVoiceSource(
        None, TtsProviderChain([provider, backup]), pricing=((provider, binding), (backup, binding))
    )
    manager._voice_source = source
    assert manager._speech_delivery is not None
    cast(SqlSpeechDelivery, manager._speech_delivery).pricing = source
    try:
        assert await manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
        assert provider.calls == backup.calls == 1
        async with storage.database.sessions() as sql:
            costs = list(await sql.scalars(select(ModelCostRecord)))
            assert len(costs) == 2 and sum(cost.charged_micros or 0 for cost in costs) == 4000
            roots = list(
                await sql.scalars(
                    select(TaskRunRecord).where(TaskRunRecord.parent_run_id.is_(None))
                )
            )
            assert len(roots) == 1 and roots[0].status == "succeeded"
    finally:
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


class WaitingAudio(AudioStream):
    def __init__(self) -> None:
        super().__init__(None)
        self.entered = asyncio.Event()

    async def __anext__(self) -> bytes:
        if self.reads == 0:
            return await super().__anext__()
        self.entered.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration


class AudioSocket(RecordingWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.audio_sent = asyncio.Event()

    async def send_bytes(self, value: bytes) -> None:
        await super().send_bytes(value)
        self.audio_sent.set()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["cancelled", "expired", "rebind", "privacy"])
async def test_proactive_wait_is_stopped_with_no_late_end_or_replacement(
    backend: str, change: str, tmp_path: Path
) -> None:
    socket = AudioSocket()
    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), socket=socket
    )
    stream = WaitingAudio()
    provider.stream = stream
    task = asyncio.create_task(
        manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
    )
    try:
        await asyncio.wait_for(stream.entered.wait(), 3)
        await asyncio.wait_for(socket.audio_sent.wait(), 3)
        if change == "rebind":
            from app.ids import uuid7

            session.conversation_id = uuid7()
        elif change == "privacy":
            session.privacy_level = PrivacyLevel.L2
        else:
            async with storage.database.sessions.begin() as sql:
                root = await sql.scalar(
                    select(TaskRunRecord).where(TaskRunRecord.parent_run_id.is_(None))
                )
                assert root is not None
                if change == "cancelled":
                    root.status = "cancelled"
                else:
                    root.deadline = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(BudgetDenied):
            await asyncio.wait_for(task, 3)
        assert provider.calls == stream.closed == 1
        assert len(socket.audio) == 1
        assert not any(frame["type"] == "voice.sentence.end" for frame in socket.texts)
        async with storage.database.sessions() as sql:
            root = await sql.scalar(
                select(TaskRunRecord).where(TaskRunRecord.parent_run_id.is_(None))
            )
            assert root is not None and root.status in {"failed", "cancelled"}
            fee = await sql.scalar(select(ModelCostRecord))
            assert fee is not None and fee.state == "unknown" and fee.charged_micros == 2000
        chain = cast(StaticVoiceSource, manager._voice_source)._tts_chain
        assert chain is not None and not chain._blocked_until
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("method", ["disconnect", "interrupt"])
async def test_proactive_lifecycle_cancels_and_joins_owned_work(
    backend: str, method: str, tmp_path: Path
) -> None:
    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused")
    )
    stream = WaitingAudio()
    provider.stream = stream
    task = asyncio.create_task(
        manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
    )
    try:
        await asyncio.wait_for(stream.entered.wait(), 3)
        if method == "disconnect":
            await asyncio.wait_for(manager.disconnect(session), 3)
        else:
            await asyncio.wait_for(manager._interrupt(session, reason="synthetic_interrupt"), 3)
        assert task.done() and task.cancelled()
        assert session.proactive_task is None and session.proactive_generation_id is None
        assert provider.calls == stream.closed == 1
        async with storage.database.sessions() as sql:
            root = await sql.scalar(
                select(TaskRunRecord).where(TaskRunRecord.parent_run_id.is_(None))
            )
            assert root is not None and root.status == "cancelled"
            fee = await sql.scalar(select(ModelCostRecord))
            assert fee is not None and fee.charged_micros == 2000
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_repeated_disconnect_cancellation_waits_for_proactive_terminal_sql(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused")
    )
    stream = WaitingAudio()
    provider.stream = stream
    entered, release = asyncio.Event(), asyncio.Event()
    original = _Delivery.finish

    async def finish(context: _Delivery, status: str, reason: str) -> None:
        entered.set()
        await release.wait()
        await original(context, status, reason)

    monkeypatch.setattr(_Delivery, "finish", finish)
    task = asyncio.create_task(
        manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
    )
    close = None
    try:
        await asyncio.wait_for(stream.entered.wait(), 3)
        close = asyncio.create_task(manager.disconnect(session))
        await asyncio.wait_for(entered.wait(), 3)
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        assert not close.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(close, 3)
        assert task.done() and task.cancelled() and session.proactive_task is None
        async with storage.database.sessions() as sql:
            root = await sql.scalar(
                select(TaskRunRecord).where(TaskRunRecord.parent_run_id.is_(None))
            )
            assert root is not None and root.status == "cancelled"
        assert stream.closed == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *([close] if close else []), return_exceptions=True)
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("mode", ["text_only", "empty_speech", "tts_failure"])
async def test_proactive_text_delivery_is_distinct_from_selected_audio_failure(
    backend: str, mode: str, tmp_path: Path
) -> None:
    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), tts=mode != "text_only"
    )
    if mode == "tts_failure":
        provider.stream = AudioStream(RuntimeError("synthetic"))
    text = "```\nprivate code\n```" if mode == "empty_speech" else "synthetic speech"
    try:
        assert await manager._send_proactive(session, text, PrivacyLevel.L1)
        async with storage.database.sessions() as sql:
            root = await sql.scalar(
                select(TaskRunRecord).where(TaskRunRecord.parent_run_id.is_(None))
            )
            assert root is not None and root.status == (
                "failed" if mode == "tts_failure" else "succeeded"
            )
            costs = list(await sql.scalars(select(ModelCostRecord)))
            assert len(costs) == int(mode == "tts_failure")
        assert provider.calls == int(mode == "tts_failure")
        assert cast(RecordingWebSocket, session.websocket).texts[0]["type"] == "proactive.committed"
    finally:
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_proactive_ephemeral_connection_under_cost_limit_creates_no_run_or_request(
    backend: str, tmp_path: Path
) -> None:
    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused")
    )
    session.privacy_level = PrivacyLevel.L3
    try:
        with pytest.raises(BudgetDenied, match="ephemeral_operation_run_forbidden"):
            await manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
        assert provider.calls == 0
        async with storage.database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))
    finally:
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_expired_proactive_root_is_recovered_without_replay_or_refund(
    backend: str, tmp_path: Path
) -> None:
    from app.ids import uuid7

    storage, manager, session, service, provider, _, config = await fixture(
        backend, tmp_path, FakeRecognizer("unused")
    )
    root_id = uuid7()
    try:
        async with storage.database.sessions.begin() as sql:
            sql.add(
                TaskRunRecord(
                    id=root_id,
                    user_id=session.principal.user_id,
                    conversation_id=session.conversation_id,
                    request_id=f"synthetic:{root_id}",
                    status="running",
                    privacy_level="L1",
                    config_version=config.current.version,
                    budget=config.current.config.run_budget.model_dump(mode="json"),
                    deadline=datetime.now(UTC) - timedelta(seconds=1),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    contract={
                        "entry": "voice.proactive_output",
                        "criterion": "text_with_optional_audio_sent",
                        "required_work": [],
                    },
                )
            )
        assert await recover_expired_speech_deliveries(storage.database) == 1
        assert await recover_expired_speech_deliveries(storage.database) == 0
        assert provider.calls == 0
        async with storage.database.sessions() as sql:
            root = await sql.get_one(TaskRunRecord, root_id)
            assert root.status == "failed" and goal_view(root, [], [], []).status == "inconclusive"
    finally:
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_proactive_locked_frame_rechecks_live_binding_after_root_query_wait(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ids import uuid7

    socket = RecordingWebSocket()
    storage, manager, session, service, provider, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), socket=socket
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = _Delivery.validate

    async def validate(context: _Delivery) -> None:
        await original(context)
        if session.send_lock.locked() and provider.calls:
            entered.set()
            await release.wait()

    monkeypatch.setattr(_Delivery, "validate", validate)
    task = asyncio.create_task(
        manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        session.conversation_id = uuid7()
        release.set()
        with pytest.raises(BudgetDenied, match="voice_source_changed"):
            await asyncio.wait_for(task, 3)
        assert socket.texts and socket.texts[0]["type"] == "proactive.committed"
        assert not any(frame["type"] == "voice.sentence" for frame in socket.texts)
        assert not socket.audio and provider.stream.closed == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["rebind", "privacy"])
async def test_reactive_locked_transcript_rechecks_binding_after_root_query_wait(
    backend: str, change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = RecordingWebSocket()
    recognizer = FakeRecognizer("unused")
    storage, manager, session, service, _, _, _ = await fixture(
        backend, tmp_path, recognizer, socket=socket
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = _Delivery.validate

    async def validate(context: _Delivery) -> None:
        await original(context)
        active = session.turn_delivery
        if session.send_lock.locked() and active is not None and active.run_id == context.run_id:
            entered.set()
            await release.wait()

    monkeypatch.setattr(_Delivery, "validate", validate)
    source = cast(StaticVoiceSource, manager._voice_source)
    task = asyncio.create_task(
        manager._run_utterance(
            session,
            b"",
            recognizer,
            source._tts_chain,
            prefetched_transcript="synthetic transcript",
        )
    )
    session.turn_task = task
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if change == "rebind":
            session.conversation_id = uuid7()
        else:
            session.privacy_level = PrivacyLevel.L2
        release.set()
        await asyncio.wait_for(task, 3)
        assert not any(frame["type"] == "voice.transcript" for frame in socket.texts)
        assert not socket.audio
        async with storage.database.sessions() as sql:
            root = await sql.scalar(select(TaskRunRecord))
            assert root is not None and root.status == "failed"
            assert root.contract["delivery_result"] == (
                "voice_source_changed" if change == "rebind" else "voice_privacy_changed"
            )
            assert not list(await sql.scalars(select(ModelCostRecord)))
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()
