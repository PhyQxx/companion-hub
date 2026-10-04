"""Synthetic device speech has a delivery root and conservatively owned quotes."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import JsonValue
from sqlalchemy import select, update
from test_voice_budget_terminal import AudioStream, Synthesizer
from test_voice_recipient_guard import fixture as recipient_fixture
from test_voice_websocket import StreamingBackend, config_yaml

from app.api.voice_ws import VoiceWebSocketManager
from app.chat import ChatService
from app.config import DatabaseConfigStore
from app.db import ModelCostRecord, TaskRunRecord
from app.harness.budget import BudgetDenied
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.harness.voice_sources import VoiceRecipientClaim
from app.runs.speech_delivery import SqlSpeechDelivery
from app.runs.voice_sources import SqlVoiceSourceGuard
from app.voice.contracts import SpeechSynthesizer
from app.voice.delivery_ports import VoiceProviderBinding
from app.voice.factory import StaticVoiceSource
from app.voice.failover import TtsProviderChain
from scripts.benchmark_storage import FixtureStorage


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_ephemeral_speech_does_not_create_durable_runs(backend: str, tmp_path: Path) -> None:
    from dataclasses import replace

    from app.schemas import PrivacyLevel

    provider = Synthesizer(AudioStream(None))
    storage, recipient, manager, _, _ = await fixture(backend, tmp_path, [provider])

    async def emit(kind: str, payload: dict[str, JsonValue]) -> None:
        pass

    try:
        with pytest.raises(BudgetDenied, match="ephemeral_operation_run_forbidden"):
            await manager.stream_device_speech(
                replace(recipient, privacy_level=PrivacyLevel.L3), "synthetic", emit
            )
        assert provider.calls == 0
        async with storage.database.sessions() as session:
            assert not list(await session.scalars(select(TaskRunRecord)))
            assert not list(await session.scalars(select(ModelCostRecord)))
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_partial_audio_is_failed_delivery_and_keeps_unknown_fee(
    backend: str, tmp_path: Path
) -> None:
    first = Synthesizer(AudioStream(RuntimeError("synthetic"), late=True))
    backup = Synthesizer(AudioStream(None))
    storage, recipient, manager, _, chain = await fixture(backend, tmp_path, [first, backup])
    frames: list[str] = []

    async def emit(kind: str, payload: dict[str, JsonValue]) -> None:
        frames.append(kind)

    try:
        assert not await manager.stream_device_speech(recipient, "synthetic", emit)
        assert backup.calls == 0 and id(first) in chain._blocked_until
        assert "pet.audio.chunk" in frames and "pet.audio.end" not in frames
        async with storage.database.sessions() as session:
            rows = list(await session.scalars(select(TaskRunRecord)))
            assert len(rows) == 2 and all(row.status == "failed" for row in rows)
            fees = list(await session.scalars(select(ModelCostRecord)))
            assert len(fees) == 1 and fees[0].state == "unknown" and fees[0].charged_micros == 2000
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_repeated_cancellation_joins_terminal_sql_cleanup(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.runs.speech_delivery import _Delivery

    provider = Synthesizer(AudioStream(None))
    storage, recipient, manager, _, _ = await fixture(backend, tmp_path, [provider])
    end, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = _Delivery.finish

    async def finish(context: _Delivery, status: str, reason: str) -> None:
        cleanup.set()
        await release.wait()
        await original(context, status, reason)

    monkeypatch.setattr(_Delivery, "finish", finish)

    async def emit(kind: str, payload: dict[str, JsonValue]) -> None:
        if kind == "pet.audio.end":
            end.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(manager.stream_device_speech(recipient, "synthetic", emit))
    try:
        await asyncio.wait_for(end.wait(), 3)
        task.cancel()
        await asyncio.wait_for(cleanup.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        async with storage.database.sessions() as session:
            root = await session.scalar(
                select(TaskRunRecord).where(
                    TaskRunRecord.contract["criterion"].as_string() == "audio_frames_sent"
                )
            )
            assert root is not None and root.status == "cancelled"
            fee = await session.scalar(select(ModelCostRecord))
            assert fee is not None and fee.state == "unknown" and fee.charged_micros == 2000
        assert provider.stream.closed == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_provider_revocation_stops_waiting_delivery_without_fallback(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, backup = Synthesizer(AudioStream(None)), Synthesizer(AudioStream(None))
    storage, recipient, manager, _, chain = await fixture(backend, tmp_path, [first, backup])
    entered, release = asyncio.Event(), asyncio.Event()
    frames: list[str] = []
    revoked = False
    original = StaticVoiceSource.validate_provider

    def validate(source: StaticVoiceSource, provider: object) -> None:
        original(source, provider)
        if revoked and provider is first:
            raise BudgetDenied("voice_provider_configuration_changed")

    monkeypatch.setattr(StaticVoiceSource, "validate_provider", validate)

    async def emit(kind: str, payload: dict[str, JsonValue]) -> None:
        if kind == "pet.audio.end":
            entered.set()
            await release.wait()
        frames.append(kind)

    task = asyncio.create_task(manager.stream_device_speech(recipient, "synthetic", emit))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        revoked = True
        with pytest.raises(BudgetDenied, match="voice_provider_configuration_changed"):
            await asyncio.wait_for(task, 3)
        assert backup.calls == 0 and not chain._blocked_until
        assert "pet.audio.end" not in frames and frames.count("pet.audio.failed") == 1
        async with storage.database.sessions() as session:
            root = await session.scalar(
                select(TaskRunRecord).where(
                    TaskRunRecord.contract["criterion"].as_string() == "audio_frames_sent"
                )
            )
            assert root is not None and root.status == "failed"
            fee = await session.scalar(select(ModelCostRecord))
            assert fee is not None and fee.state == "unknown" and fee.charged_micros == 2000
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()


async def fixture(
    backend: str,
    tmp_path: Path,
    providers: list[Synthesizer],
    *,
    priced: bool = True,
    enabled: bool = True,
    limit: float | None = 0.01,
) -> tuple[
    FixtureStorage,
    VoiceRecipientClaim,
    VoiceWebSocketManager,
    DatabaseConfigStore,
    TtsProviderChain,
]:
    storage, recipient = await recipient_fixture(backend, tmp_path, "avatar.chat")
    try:
        path = tmp_path / "device-speech.yaml"
        path.write_text(
            config_yaml()
            + f"\nrun_budget:\n  enabled: {str(enabled).lower()}\n"
            + f"  cost_currency: CNY\n  max_daily_cost: {limit if limit is not None else 'null'}\n"
        )
        config = DatabaseConfigStore(storage.database, path)
        await config.load()
        synthesizers: list[SpeechSynthesizer] = list(providers)
        chain = TtsProviderChain(synthesizers)
        quote = UnitCostQuote(
            pricing=UnitPricing(unit="request", currency="CNY", rate_per_unit=Decimal(".002")),
            maximum_quantity=1,
        )
        source = StaticVoiceSource(
            None,
            chain,
            pricing=tuple(
                (provider, VoiceProviderBinding(f"synthetic.tts.{index}", quote))
                for index, provider in enumerate(providers)
            )
            if priced
            else (),
        )
        guard = SqlVoiceSourceGuard(storage.database)
        port = SqlSpeechDelivery(storage.database, config, guard, source)
        service = ChatService(storage.database, config, router_builder=lambda _: StreamingBackend())
        manager = VoiceWebSocketManager(
            service, voice_source=source, source_guard=guard, speech_delivery=port
        )
        return storage, recipient, manager, config, chain
    except BaseException:
        await storage.close()
        raise


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_delivery_root_remains_active_until_final_frame_is_sent(
    backend: str, enabled: bool, tmp_path: Path
) -> None:
    provider = Synthesizer(AudioStream(None))
    storage, recipient, manager, _, _ = await fixture(
        backend, tmp_path, [provider], enabled=enabled, limit=0.01 if enabled else None
    )
    frames: list[str] = []
    end_started, release = asyncio.Event(), asyncio.Event()

    async def emit(kind: str, payload: dict[str, JsonValue]) -> None:
        frames.append(kind)
        if kind == "pet.audio.end":
            end_started.set()
            await release.wait()

    task = asyncio.create_task(manager.stream_device_speech(recipient, "synthetic text", emit))
    try:
        await asyncio.wait_for(end_started.wait(), 3)
        async with storage.database.sessions() as session:
            runs = list(await session.scalars(select(TaskRunRecord)))
            root = next(row for row in runs if row.contract["criterion"] == "audio_frames_sent")
            child = next(
                row for row in runs if row.contract["criterion"] == "provider_response_returned"
            )
            assert root.status == "running" and child.status == "succeeded"
            assert child.parent_run_id == root.id
            cost = await session.get_one(ModelCostRecord, child.id)
            assert cost.state == "unknown" and cost.charged_micros == 2000
            assert cost.unit == "request" and cost.unit_quantity is None
            assert "synthetic text" not in str(root.contract) + str(child.contract)
        release.set()
        assert await asyncio.wait_for(task, 3)
        async with storage.database.sessions() as session:
            assert (await session.get_one(TaskRunRecord, root.id)).status == "succeeded"
        assert frames[-1] == "pet.audio.end" and provider.calls == provider.stream.closed == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_missing_quote_under_monetary_limit_does_not_dispatch_or_fallback(
    backend: str, tmp_path: Path
) -> None:
    first, backup = Synthesizer(AudioStream(None)), Synthesizer(AudioStream(None))
    storage, recipient, manager, _, chain = await fixture(
        backend, tmp_path, [first, backup], priced=False
    )
    frames: list[str] = []

    async def emit(kind: str, payload: dict[str, JsonValue]) -> None:
        frames.append(kind)

    try:
        with pytest.raises(BudgetDenied, match="voice_cost_estimate_unavailable"):
            await manager.stream_device_speech(recipient, "synthetic text", emit)
        assert first.calls == backup.calls == 0 and not chain._blocked_until
        assert frames == ["pet.audio.failed"]
        async with storage.database.sessions() as session:
            runs = list(await session.scalars(select(TaskRunRecord)))
            assert len(runs) == 1 and runs[0].status == "failed"
            assert not list(await session.scalars(select(ModelCostRecord)))
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("late", [False, True])
async def test_terminal_audio_denial_keeps_quote_and_failed_delivery_without_end(
    backend: str, late: bool, tmp_path: Path
) -> None:
    error = BudgetDenied("synthetic_audio_denied")
    first, backup = Synthesizer(AudioStream(error, late=late)), Synthesizer(AudioStream(None))
    storage, recipient, manager, _, chain = await fixture(backend, tmp_path, [first, backup])
    frames: list[str] = []

    async def emit(kind: str, payload: dict[str, JsonValue]) -> None:
        frames.append(kind)

    try:
        with pytest.raises(BudgetDenied) as caught:
            await manager.stream_device_speech(recipient, "synthetic text", emit)
        assert caught.value is error and backup.calls == 0 and not chain._blocked_until
        assert "pet.audio.end" not in frames and frames.count("pet.audio.failed") == 1
        assert first.calls == first.stream.closed == 1
        async with storage.database.sessions() as session:
            runs = list(await session.scalars(select(TaskRunRecord)))
            assert len(runs) == 2 and all(row.status == "failed" for row in runs)
            fees = list(await session.scalars(select(ModelCostRecord)))
            assert len(fees) == 1 and fees[0].state == "unknown" and fees[0].charged_micros == 2000
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_ordinary_pre_chunk_failure_reserves_each_actual_provider_attempt(
    backend: str, tmp_path: Path
) -> None:
    first, backup = (
        Synthesizer(AudioStream(RuntimeError("synthetic"))),
        Synthesizer(AudioStream(None)),
    )
    storage, recipient, manager, _, chain = await fixture(backend, tmp_path, [first, backup])
    frames: list[str] = []

    async def emit(kind: str, payload: dict[str, JsonValue]) -> None:
        frames.append(kind)

    try:
        assert await manager.stream_device_speech(recipient, "synthetic", emit)
        assert first.calls == backup.calls == first.stream.closed == backup.stream.closed == 1
        assert id(first) in chain._blocked_until
        async with storage.database.sessions() as session:
            runs = list(await session.scalars(select(TaskRunRecord)))
            root = next(row for row in runs if row.contract["criterion"] == "audio_frames_sent")
            children = [row for row in runs if row.parent_run_id == root.id]
            assert root.status == "succeeded" and {row.status for row in children} == {
                "failed",
                "succeeded",
            }
            fees = list(await session.scalars(select(ModelCostRecord)))
            assert len(fees) == 2 and sum(row.charged_micros or 0 for row in fees) == 4000
            assert all(row.state == "unknown" for row in fees)
        assert frames[-1] == "pet.audio.end"
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_unknown_failed_attempt_uses_cap_before_backup_dispatch(
    backend: str, tmp_path: Path
) -> None:
    first, backup = (
        Synthesizer(AudioStream(RuntimeError("synthetic"))),
        Synthesizer(AudioStream(None)),
    )
    storage, recipient, manager, _, chain = await fixture(
        backend, tmp_path, [first, backup], limit=0.002
    )

    async def emit(kind: str, payload: dict[str, JsonValue]) -> None:
        pass

    try:
        with pytest.raises(BudgetDenied):
            await manager.stream_device_speech(recipient, "synthetic", emit)
        assert first.calls == 1 and backup.calls == 0
        assert id(first) in chain._blocked_until and id(backup) not in chain._blocked_until
        async with storage.database.sessions() as session:
            fees = list(await session.scalars(select(ModelCostRecord)))
            assert len(fees) == 1 and fees[0].state == "unknown" and fees[0].charged_micros == 2000
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["cancel", "revoke", "tighten"])
async def test_delivery_stops_while_final_frame_is_waiting(
    backend: str, change: str, tmp_path: Path
) -> None:
    from app.config.models import RunBudgetConfig
    from app.db import DeviceClientRecord

    provider = Synthesizer(AudioStream(None))
    storage, recipient, manager, config, _ = await fixture(backend, tmp_path, [provider])
    entered, release = asyncio.Event(), asyncio.Event()
    frames: list[str] = []

    async def emit(kind: str, payload: dict[str, JsonValue]) -> None:
        if kind == "pet.audio.end":
            entered.set()
            await release.wait()
        frames.append(kind)

    task = asyncio.create_task(manager.stream_device_speech(recipient, "synthetic", emit))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with storage.database.sessions() as session:
            root = await session.scalar(
                select(TaskRunRecord).where(
                    TaskRunRecord.contract["criterion"].as_string() == "audio_frames_sent"
                )
            )
            assert root is not None
        if change == "tighten":
            candidate = config.current.config.model_copy(
                update={"run_budget": RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.001)}
            )
            draft = await config.create_draft(candidate, actor="synthetic")
            await config.publish(draft.version, actor="synthetic")
        else:
            async with storage.database.sessions.begin() as session:
                if change == "cancel":
                    await session.execute(
                        update(TaskRunRecord)
                        .where(TaskRunRecord.id == root.id)
                        .values(status="cancelled")
                    )
                else:
                    await session.execute(
                        update(DeviceClientRecord)
                        .where(DeviceClientRecord.id == recipient.device_id)
                        .values(revoked_at=datetime.now(UTC))
                    )
        with pytest.raises(BudgetDenied):
            await asyncio.wait_for(task, 3)
        assert "pet.audio.end" not in frames and frames.count("pet.audio.failed") == 1
        assert provider.calls == provider.stream.closed == 1
        async with storage.database.sessions() as session:
            cost = await session.scalar(select(ModelCostRecord))
            assert cost is not None and cost.state == "unknown" and cost.charged_micros == 2000
            closed_root = await session.get_one(TaskRunRecord, root.id)
            assert closed_root.status == ("cancelled" if change == "cancel" else "failed")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_speech_trace_children_share_an_existing_root_budget(
    backend: str, tmp_path: Path
) -> None:
    from datetime import timedelta

    from app.config.models import RunBudgetConfig
    from app.harness.budget import budget_scope
    from app.ids import uuid7
    from app.runs.budget import RunModelBudget

    provider = Synthesizer(AudioStream(None))
    storage, recipient, manager, _, _ = await fixture(backend, tmp_path, [provider])
    parent = uuid7()
    config = RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.01)
    async with storage.database.sessions.begin() as session:
        session.add(
            TaskRunRecord(
                id=parent,
                user_id=recipient.user_id,
                status="running",
                privacy_level="L1",
                contract={},
                budget=config.model_dump(mode="json"),
                deadline=datetime.now(UTC) + timedelta(minutes=1),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    async def emit(kind: str, payload: dict[str, JsonValue]) -> None:
        pass

    try:
        with budget_scope(
            RunModelBudget(
                storage.database, run_id=parent, user_id=recipient.user_id, config=config
            )
        ):
            assert await manager.stream_device_speech(recipient, "synthetic", emit)
        async with storage.database.sessions() as session:
            ancestor = await session.get_one(TaskRunRecord, parent)
            root = await session.scalar(
                select(TaskRunRecord).where(
                    TaskRunRecord.contract["criterion"].as_string() == "audio_frames_sent"
                )
            )
            assert root is not None and root.parent_run_id == parent
            child = await session.scalar(
                select(TaskRunRecord).where(
                    TaskRunRecord.contract["entry"].as_string() == "voice.tts"
                )
            )
            assert child is not None and child.parent_run_id == root.id
            assert ancestor.contract["resource_usage"]["tool_attempts"] == 1
            assert "resource_usage" not in root.contract
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_recovery_fails_only_expired_speech_scopes_and_keeps_unknown_fee(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from app.runs.speech_delivery import _Delivery, recover_expired_speech_deliveries

    provider = Synthesizer(AudioStream(None))
    storage, recipient, manager, _, _ = await fixture(backend, tmp_path, [provider])
    entered, release = asyncio.Event(), asyncio.Event()
    validation_lock = asyncio.Lock()
    original_validate = _Delivery.validate

    async def validate(context: _Delivery) -> None:
        async with validation_lock:
            await original_validate(context)

    monkeypatch.setattr(_Delivery, "validate", validate)

    async def emit(kind: str, payload: dict[str, JsonValue]) -> None:
        if kind == "pet.audio.end":
            entered.set()
            await release.wait()

    task = asyncio.create_task(manager.stream_device_speech(recipient, "synthetic", emit))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with storage.database.sessions() as session:
            root = await session.scalar(
                select(TaskRunRecord).where(
                    TaskRunRecord.contract["criterion"].as_string() == "audio_frames_sent"
                )
            )
            assert root is not None
        # Recovery owns this transition; keep the live delivery watcher from
        # racing the same expiry and legitimately finishing the row first.
        async with validation_lock:
            async with storage.database.sessions.begin() as session:
                await session.execute(
                    update(TaskRunRecord)
                    .where(TaskRunRecord.id == root.id)
                    .values(deadline=datetime.now(UTC) - timedelta(seconds=1))
                )
            assert await recover_expired_speech_deliveries(storage.database) == 1
        assert await recover_expired_speech_deliveries(storage.database) == 0
        with pytest.raises(BudgetDenied):
            await asyncio.wait_for(task, 3)
        async with storage.database.sessions() as session:
            row = await session.get_one(TaskRunRecord, root.id)
            assert (
                row.status == "failed"
                and row.contract["delivery_result"] == "expired_delivery_unknown"
            )
            fee = await session.scalar(select(ModelCostRecord))
            assert fee is not None and fee.charged_micros == 2000 and fee.state == "unknown"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()
