"""Real SQL voice roots with synthetic recognition, routing and output."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi import WebSocket
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_llm import FakeProvider
from test_voice_budget_terminal import AudioStream, Synthesizer
from test_voice_source_guard import source_fixture
from test_voice_websocket import (
    FakeRecognizer,
    FakeStreamingRecognizer,
    RecordingWebSocket,
    config_yaml,
)

from app.api.voice_ws import VoiceSession, VoiceWebSocketManager
from app.auth import ChatPrincipal
from app.chat import ChatService
from app.config import DatabaseConfigStore
from app.db import MessageRecord, ModelCostRecord, ModelReservationRecord, TaskRunRecord
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.llm import LLMRouter
from app.runs.resources import resource_usage
from app.runs.speech_delivery import SqlSpeechDelivery
from app.runs.store import transition_run
from app.runs.voice_sources import SqlVoiceSourceGuard
from app.runs.voice_turn import SqlVoiceTurnDelivery
from app.schemas import PrivacyLevel
from app.voice import EnergyVad
from app.voice.contracts import SpeechRecognizer
from app.voice.delivery_ports import VoiceProviderBinding
from app.voice.factory import StaticVoiceSource
from app.voice.failover import TtsProviderChain
from scripts.benchmark_storage import FixtureStorage


async def fixture(
    backend: str,
    tmp_path: Path,
    recognizer: SpeechRecognizer,
    *,
    enabled: bool = True,
    priced: bool = True,
    tts: bool = True,
    socket: RecordingWebSocket | None = None,
) -> tuple[
    FixtureStorage,
    VoiceWebSocketManager,
    VoiceSession,
    ChatService,
    Synthesizer,
    FakeProvider,
    DatabaseConfigStore,
]:
    storage, claim = await source_fixture(backend, tmp_path)
    try:
        path = tmp_path / "voice-root.yaml"
        path.write_text(
            config_yaml().replace(
                "output_cost_per_million: 0", "output_cost_per_million: 0\n    cost_currency: CNY"
            )
            + f"\nrun_budget:\n  enabled: {str(enabled).lower()}\n  cost_currency: CNY\n"
            + f"  max_daily_cost: {'0.02' if enabled else 'null'}\n"
        )
        config = DatabaseConfigStore(storage.database, path)
        await config.load()
        synthesizer = Synthesizer(AudioStream(None))
        chain = TtsProviderChain([synthesizer]) if tts else None

        def binding(kind: str, cost: str) -> VoiceProviderBinding:
            return VoiceProviderBinding(
                f"synthetic.voice.{kind}",
                UnitCostQuote(
                    pricing=UnitPricing(
                        unit="request", currency="CNY", rate_per_unit=Decimal(cost)
                    ),
                    maximum_quantity=1,
                ),
            )

        source = StaticVoiceSource(
            recognizer,
            chain,
            pricing=((recognizer, binding("asr", ".003")), (synthesizer, binding("tts", ".002")))
            if priced
            else (),
        )
        guard = SqlVoiceSourceGuard(storage.database)
        speech = SqlSpeechDelivery(storage.database, config, guard, source)
        provider = FakeProvider("cloud")
        service = ChatService(
            storage.database,
            config,
            router_builder=lambda snapshot: LLMRouter(
                endpoints=snapshot.models,
                routes=snapshot.routes,
                providers={"cloud": provider, "local": FakeProvider("local")},
            ),
        )
        manager = VoiceWebSocketManager(
            service,
            voice_source=source,
            source_guard=guard,
            speech_delivery=speech,
            voice_turn_delivery=SqlVoiceTurnDelivery(speech),
        )
        session = VoiceSession(
            websocket=cast(WebSocket, socket or RecordingWebSocket()),
            principal=ChatPrincipal(
                session_id=claim.actor_id,
                user_id=claim.user_id,
                display_name="Synthetic",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
            conversation_id=claim.conversation_id,
            privacy_level=PrivacyLevel.L1,
            vad=EnergyVad(),
            wake_word=None,
        )
        return storage, manager, session, service, synthesizer, provider, config
    except BaseException:
        await storage.close()
        raise


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("drop", ["noise", "privacy", "disconnect", "rebind"])
async def test_dropped_stream_capture_closes_request_and_root(
    backend: str, drop: str, tmp_path: Path
) -> None:
    recognizer = FakeStreamingRecognizer()
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer, tts=False
    )
    try:
        session.collecting = True
        session.utterance.extend(b"\x01\x00" * 16)
        await manager._start_streamer(session, b"\x01\x00" * 16)
        assert session.capture_delivery is not None
        identifier = session.capture_delivery.run_id
        if drop == "noise":
            await manager._finalize_utterance(session, explicit=True)
        elif drop == "privacy":
            session.privacy_level = PrivacyLevel.L0
            await manager._feed_streamer(session, b"\x01\x00" * 16)
        elif drop == "disconnect":
            await manager.disconnect(session)
        else:
            conversation = await service.create_conversation(
                user_id=session.principal.user_id, title="Synthetic rebound"
            )
            await manager._on_hello(
                session,
                {
                    "format": "pcm_s16le",
                    "sample_rate": 16000,
                    "channels": 1,
                    "conversation_id": str(conversation.id),
                    "privacy_level": "L0",
                },
            )
            assert session.conversation_id == conversation.id
        assert session.capture_delivery is session.turn_delivery is None
        assert session.streamer_request is None and not session.delivery_tasks
        assert recognizer.fed_frames == 1 and recognizer.finalize_calls == 0
        assert not provider.requests and recognizer.transcribe_calls == 0
        async with storage.database.sessions() as sql:
            root = await sql.get_one(TaskRunRecord, identifier)
            children = list(
                await sql.scalars(
                    select(TaskRunRecord).where(TaskRunRecord.parent_run_id == identifier)
                )
            )
            assert (
                root.status == "cancelled"
                and len(children) == 1
                and children[0].status == "cancelled"
            )
            costs = list(await sql.scalars(select(ModelCostRecord)))
            assert (
                len(costs) == 1 and costs[0].state == "unknown" and costs[0].charged_micros == 3000
            )
    finally:
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_before_start_turn_cancel_closes_transferred_capture_root(
    backend: str, tmp_path: Path
) -> None:
    recognizer = FakeStreamingRecognizer()
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer, tts=False
    )
    try:
        pcm = b"\x01\x00" * 16000
        session.collecting = True
        session.utterance.extend(pcm)
        await manager._start_streamer(session, pcm)
        assert session.capture_delivery is not None
        identifier = session.capture_delivery.run_id
        await manager._finalize_utterance(session, explicit=True)
        task = session.turn_task
        assert task is not None
        await manager._cancel_turn_task(session, task)
        assert task.cancelled() and session.turn_task is None and session.turn_delivery is None
        assert not provider.requests
        async with storage.database.sessions() as sql:
            assert (await sql.get_one(TaskRunRecord, identifier)).status == "cancelled"
            assert not list(await sql.scalars(select(MessageRecord)))
    finally:
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_prefetch_cancel_and_full_retry_do_not_create_new_root(
    backend: str, tmp_path: Path
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Recognizer(FakeRecognizer):
        async def transcribe(self, pcm: bytes, *, sample_rate: int, language: str | None) -> str:
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await release.wait()
            return "synthetic utterance"

    recognizer = Recognizer("synthetic")
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer, tts=False
    )
    task = None
    try:
        pcm = b"\x01\x00" * 16000
        session.collecting = True
        session.utterance.extend(pcm)
        await manager._start_asr_prefetch(session)
        await asyncio.wait_for(entered.wait(), 5)
        assert session.capture_delivery is not None
        identifier = session.capture_delivery.run_id
        manager._discard_asr_prefetch(session)
        if session.asr_tasks:
            await asyncio.gather(*tuple(session.asr_tasks), return_exceptions=True)
        await manager._finalize_utterance(session, explicit=True)
        task = session.turn_task
        assert task is not None
        await asyncio.wait_for(task, 5)
        async with storage.database.sessions() as sql:
            roots = list(
                await sql.scalars(
                    select(TaskRunRecord).where(TaskRunRecord.parent_run_id.is_(None))
                )
            )
            assert len(roots) == 1 and roots[0].id == identifier and roots[0].status == "succeeded"
            assert resource_usage(roots[0])["tool_attempts"] == 2
            costs = list(
                await sql.scalars(select(ModelCostRecord).where(ModelCostRecord.unit == "request"))
            )
            assert len(costs) == 2 and sum(row.charged_micros or 0 for row in costs) == 6000
        assert recognizer.calls == 2 and len(provider.requests) == 1
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_fallback_waits_for_failed_stream_request_sql_terminal(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.runs import operation

    entered, release = asyncio.Event(), asyncio.Event()
    recognizer = FakeStreamingRecognizer(finalize_error=True)
    storage, manager, session, service, _, _, _ = await fixture(
        backend, tmp_path, recognizer, tts=False
    )
    original = transition_run

    async def transition(sql: AsyncSession, identifier: UUID, status: str) -> None:
        if status == "failed":
            entered.set()
            await release.wait()
        await original(sql, identifier, status)

    monkeypatch.setattr(operation, "transition_run", transition)
    finalize = None
    try:
        pcm = b"\x01\x00" * 16000
        session.collecting = True
        session.utterance.extend(pcm)
        await manager._start_streamer(session, pcm)
        finalize = asyncio.create_task(manager._finalize_utterance(session, explicit=True))
        await asyncio.wait_for(entered.wait(), 5)
        assert not finalize.done() and recognizer.transcribe_calls == 0
        release.set()
        await asyncio.wait_for(finalize, 5)
        task = session.turn_task
        assert task is not None
        await asyncio.wait_for(task, 5)
        async with storage.database.sessions() as sql:
            stream = await sql.scalar(
                select(TaskRunRecord).where(
                    TaskRunRecord.contract["entry"].as_string() == "voice.asr_stream"
                )
            )
            assert stream is not None and stream.status == "failed"
        assert recognizer.transcribe_calls == 1
    finally:
        release.set()
        if finalize is not None:
            await asyncio.gather(finalize, return_exceptions=True)
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_one_root_owns_recognition_chat_and_tail_audio(
    backend: str, enabled: bool, tmp_path: Path
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Socket(RecordingWebSocket):
        async def send_bytes(self, value: bytes) -> None:
            entered.set()
            await release.wait()
            await super().send_bytes(value)

    socket, recognizer = Socket(), FakeRecognizer("synthetic utterance")
    storage, manager, session, service, tts, provider, _ = await fixture(
        backend, tmp_path, recognizer, enabled=enabled, socket=socket
    )
    _, chain = await manager._voice_source.resolve()
    task = asyncio.create_task(
        manager._run_utterance(session, b"\x01\x00" * 16000, recognizer, chain)
    )
    session.turn_task = task
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with storage.database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            root = next(row for row in runs if row.contract.get("entry") == "voice.utterance")
            chat = next(row for row in runs if row.contract.get("kind") == "chat.reply")
            asr = next(row for row in runs if row.contract.get("entry") == "voice.asr")
            audio = next(row for row in runs if row.contract.get("entry") == "voice.tts")
            assert root.status == "running" and chat.status == asr.status == "succeeded"
            assert all(row.parent_run_id == root.id for row in (chat, asr, audio))
            assert root.llm_attempts == (1 if enabled else 0) and chat.llm_attempts == 0
            reservations = list(await sql.scalars(select(ModelReservationRecord)))
            assert len(reservations) == (1 if enabled else 0)
            assert all(row.run_id == root.id for row in reservations)
            assert resource_usage(root).get("tool_attempts", 0) == (2 if enabled else 0)
            assert len(list(await sql.scalars(select(MessageRecord)))) == 2
        release.set()
        await asyncio.wait_for(task, 5)
        async with storage.database.sessions() as sql:
            assert (await sql.get_one(TaskRunRecord, root.id)).status == "succeeded"
            costs = list(
                await sql.scalars(select(ModelCostRecord).where(ModelCostRecord.unit == "request"))
            )
            assert len(costs) == 2 and sum(row.charged_micros or 0 for row in costs) == 5000
            assert all(row.state == "unknown" and row.unit_quantity is None for row in costs)
        assert recognizer.calls == tts.calls == tts.stream.closed == len(provider.requests) == 1
        assert socket.texts[-1]["type"] == "reply.committed"
        assert session.turn_task is None and session.turn_delivery is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_missing_asr_quote_stops_before_recognition_or_chat(
    backend: str, tmp_path: Path
) -> None:
    recognizer = FakeRecognizer("synthetic utterance")
    storage, manager, session, service, tts, provider, _ = await fixture(
        backend, tmp_path, recognizer, priced=False
    )
    try:
        await manager._run_utterance(session, b"\x01\x00" * 16000, recognizer, None)
        assert recognizer.calls == tts.calls == 0 and not provider.requests
        socket = cast(RecordingWebSocket, session.websocket)
        assert [row["reason_code"] for row in socket.texts if row["type"] == "turn.failed"] == [
            "voice_cost_estimate_unavailable"
        ]
        async with storage.database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            assert len(runs) == 1 and runs[0].status == "failed"
            assert not list(await sql.scalars(select(ModelCostRecord)))
            assert not list(await sql.scalars(select(MessageRecord)))
    finally:
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("fallback", [False, True])
async def test_stream_decoder_is_one_request_and_fallback_uses_same_root(
    backend: str, fallback: bool, tmp_path: Path
) -> None:
    recognizer = FakeStreamingRecognizer(finalize_error=fallback)
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer, tts=False
    )
    task = None
    try:
        pcm = b"\x01\x00" * 16000
        session.utterance.extend(pcm)
        session.collecting = True
        await manager._start_streamer(session, pcm[:1024])
        await manager._feed_streamer(session, pcm[1024:2048])
        await manager._feed_streamer(session, pcm[2048:3072])
        assert session.capture_delivery is not None
        identifier = session.capture_delivery.run_id
        async with storage.database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            assert len(runs) == 2 and all(row.status == "running" for row in runs)
            fees = list(await sql.scalars(select(ModelCostRecord)))
            assert len(fees) == 1 and fees[0].charged_micros == 3000
        await manager._finalize_utterance(session, explicit=True)
        task = session.turn_task
        assert task is not None
        await asyncio.wait_for(task, 5)
        async with storage.database.sessions() as sql:
            root = await sql.get_one(TaskRunRecord, identifier)
            assert root.status == "succeeded"
            children = list(
                await sql.scalars(
                    select(TaskRunRecord).where(TaskRunRecord.parent_run_id == identifier)
                )
            )
            asr = [
                row
                for row in children
                if str(row.contract.get("entry", "")).startswith("voice.asr")
            ]
            assert len(asr) == (2 if fallback else 1)
            assert {row.status for row in asr} == (
                {"failed", "succeeded"} if fallback else {"succeeded"}
            )
            costs = list(
                await sql.scalars(select(ModelCostRecord).where(ModelCostRecord.unit == "request"))
            )
            assert sum(row.charged_micros or 0 for row in costs) == (6000 if fallback else 3000)
            assert resource_usage(root)["tool_attempts"] == (2 if fallback else 1)
        assert recognizer.fed_frames == 3 and recognizer.finalize_calls == 1
        assert recognizer.transcribe_calls == int(fallback) and len(provider.requests) == 1
        assert session.capture_delivery is None and session.turn_delivery is None
        assert session.streamer_request is None
    finally:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_root_cancellation_after_chat_commit_stops_tail_audio(
    backend: str, tmp_path: Path
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Socket(RecordingWebSocket):
        async def send_bytes(self, value: bytes) -> None:
            entered.set()
            await release.wait()
            await super().send_bytes(value)

    recognizer, socket = FakeRecognizer("synthetic utterance"), Socket()
    storage, manager, session, service, tts, _, _ = await fixture(
        backend, tmp_path, recognizer, socket=socket
    )
    _, chain = await manager._voice_source.resolve()
    task = asyncio.create_task(
        manager._run_utterance(session, b"\x01\x00" * 16000, recognizer, chain)
    )
    session.turn_task = task
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert session.turn_delivery is not None and session.turn_committed
        identifier = session.turn_delivery.run_id
        async with storage.database.sessions.begin() as sql:
            await sql.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == identifier)
                .values(status="cancelled")
            )
        await asyncio.wait_for(task, 5)
        assert not socket.audio and tts.stream.closed == 1
        assert not any(
            row["type"] in {"voice.sentence.end", "reply.committed"} for row in socket.texts
        )
        assert [row["reason_code"] for row in socket.texts if row["type"] == "turn.failed"] == [
            "budget_run_inactive"
        ]
        async with storage.database.sessions() as sql:
            chat = await sql.scalar(
                select(TaskRunRecord).where(
                    TaskRunRecord.contract["kind"].as_string() == "chat.reply"
                )
            )
            assert chat is not None and chat.status == "succeeded"
            assert len(list(await sql.scalars(select(MessageRecord)))) == 2
            costs = list(
                await sql.scalars(select(ModelCostRecord).where(ModelCostRecord.unit == "request"))
            )
            assert sum(row.charged_micros or 0 for row in costs) == 5000
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_repeated_cancel_joins_local_decoder_thread_before_root_close(
    backend: str, tmp_path: Path
) -> None:
    from threading import Event

    entered, release = Event(), Event()

    class Recognizer(FakeStreamingRecognizer):
        def feed(self, pcm: bytes) -> str | None:
            entered.set()
            release.wait(5)
            return super().feed(pcm)

    recognizer = Recognizer()
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer, tts=False
    )
    task = None
    try:
        await manager._start_streamer(session, None)
        task = asyncio.create_task(manager._feed_streamer(session, b"\x01\x00" * 16))
        assert await asyncio.to_thread(entered.wait, 3)
        assert session.capture_delivery is not None
        identifier = session.capture_delivery.run_id
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        async with storage.database.sessions() as sql:
            assert (await sql.get_one(TaskRunRecord, identifier)).status == "running"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await manager.disconnect(session)
        async with storage.database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            assert len(runs) == 2 and all(row.status == "cancelled" for row in runs)
            cost = await sql.scalar(select(ModelCostRecord))
            assert cost is not None and cost.state == "unknown" and cost.charged_micros == 3000
        assert (
            recognizer.fed_frames == 1 and recognizer.finalize_calls == 0 and not provider.requests
        )
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_stream_denial_fails_root_without_full_asr_fallback(
    backend: str, tmp_path: Path
) -> None:
    from app.harness.budget import BudgetDenied

    error = BudgetDenied("synthetic_decoder_denied")

    class Recognizer(FakeStreamingRecognizer):
        def feed(self, pcm: bytes) -> str | None:
            self.fed_frames += 1
            raise error

    recognizer = Recognizer()
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer, tts=False
    )
    try:
        with pytest.raises(BudgetDenied) as caught:
            await manager._start_streamer(session, b"\x01\x00" * 16)
        assert caught.value is error
        assert (
            recognizer.fed_frames == 1
            and recognizer.transcribe_calls == 0
            and not provider.requests
        )
        async with storage.database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            assert len(runs) == 2 and all(row.status == "failed" for row in runs)
            root = next(row for row in runs if row.contract.get("entry") == "voice.utterance")
            assert root.contract["delivery_result"] == "synthetic_decoder_denied"
            cost = await sql.scalar(select(ModelCostRecord))
            assert cost is not None and cost.state == "unknown" and cost.charged_micros == 3000
    finally:
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_stream_terminal_sql_failure_does_not_start_fallback(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.harness.budget import BudgetDenied
    from app.runs import operation

    recognizer = FakeStreamingRecognizer(finalize_error=True)
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer, tts=False
    )

    async def transition(sql: AsyncSession, identifier: UUID, status: str) -> None:
        if status == "failed":
            raise RuntimeError("synthetic terminal SQL failure")
        await transition_run(sql, identifier, status)

    monkeypatch.setattr(operation, "transition_run", transition)
    try:
        pcm = b"\x01\x00" * 16000
        session.utterance.extend(pcm)
        await manager._start_streamer(session, pcm)
        with pytest.raises(BudgetDenied, match="voice_request_settlement_failed"):
            await manager._finalize_utterance(session, explicit=True)
        assert (
            recognizer.transcribe_calls == 0 and not provider.requests and session.turn_task is None
        )
        async with storage.database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            root = next(row for row in runs if row.contract.get("entry") == "voice.utterance")
            child = next(row for row in runs if row.parent_run_id == root.id)
            assert root.status == "failed" and child.status == "running"
            assert root.contract["delivery_result"] == "voice_request_settlement_failed"
            cost = await sql.scalar(select(ModelCostRecord))
            assert cost is not None and cost.state == "unknown" and cost.charged_micros == 3000
    finally:
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_expired_capture_cannot_finalize_or_create_chat(backend: str, tmp_path: Path) -> None:
    from app.harness.budget import BudgetDenied

    recognizer = FakeStreamingRecognizer()
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer, tts=False
    )
    try:
        pcm = b"\x01\x00" * 16000
        session.utterance.extend(pcm)
        await manager._start_streamer(session, pcm)
        assert session.capture_delivery is not None
        identifier = session.capture_delivery.run_id
        async with storage.database.sessions.begin() as sql:
            await sql.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == identifier)
                .values(deadline=datetime.now(UTC) - timedelta(seconds=1))
            )
        with pytest.raises(BudgetDenied, match="run_deadline_exceeded"):
            await manager._finalize_utterance(session, explicit=True)
        assert (
            recognizer.finalize_calls == 0
            and recognizer.transcribe_calls == 0
            and not provider.requests
        )
        async with storage.database.sessions() as sql:
            assert (await sql.get_one(TaskRunRecord, identifier)).status == "failed"
            assert not list(await sql.scalars(select(MessageRecord)))
    finally:
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_text_submission_uses_voice_root_without_a_fake_asr_fee(
    backend: str, tmp_path: Path
) -> None:
    recognizer = FakeRecognizer("unused synthetic")
    storage, manager, session, service, tts, provider, _ = await fixture(
        backend, tmp_path, recognizer
    )
    task = None
    try:
        await manager._on_text_submit(session, {"text": "synthetic typed text"})
        task = session.turn_task
        assert task is not None
        await asyncio.wait_for(task, 5)
        assert recognizer.calls == 0 and tts.calls == len(provider.requests) == 1
        async with storage.database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            assert len(runs) == 3 and all(row.status == "succeeded" for row in runs)
            assert not any(
                str(row.contract.get("entry", "")).startswith("voice.asr") for row in runs
            )
            costs = list(
                await sql.scalars(select(ModelCostRecord).where(ModelCostRecord.unit == "request"))
            )
            assert len(costs) == 1 and costs[0].charged_micros == 2000
    finally:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_l3_cloud_asr_rejection_has_no_durable_voice_root(
    backend: str, tmp_path: Path
) -> None:
    recognizer = FakeRecognizer("unused synthetic")
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer
    )
    try:
        session.privacy_level = PrivacyLevel.L3
        await manager._run_utterance(session, b"\x01\x00" * 16000, recognizer, None)
        assert recognizer.calls == 0 and not provider.requests
        async with storage.database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))
    finally:
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_expiry_recovery_does_not_replay_idle_decoder_or_refund_it(
    backend: str, tmp_path: Path
) -> None:
    from app.harness.budget import BudgetDenied
    from app.runs.speech_delivery import recover_expired_speech_deliveries

    recognizer = FakeStreamingRecognizer()
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer, tts=False
    )
    try:
        await manager._start_streamer(session, b"\x01\x00" * 16)
        assert session.capture_delivery is not None
        identifier = session.capture_delivery.run_id
        async with storage.database.sessions.begin() as sql:
            await sql.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == identifier)
                .values(deadline=datetime.now(UTC) - timedelta(seconds=1))
            )
        assert await recover_expired_speech_deliveries(storage.database) == 1
        assert await recover_expired_speech_deliveries(storage.database) == 0
        with pytest.raises(BudgetDenied):
            await manager._feed_streamer(session, b"\x01\x00" * 16)
        assert (
            recognizer.fed_frames == 1 and recognizer.finalize_calls == 0 and not provider.requests
        )
        async with storage.database.sessions() as sql:
            root = await sql.get_one(TaskRunRecord, identifier)
            assert (
                root.status == "failed"
                and root.contract["delivery_result"] == "expired_delivery_unknown"
            )
            cost = await sql.scalar(select(ModelCostRecord))
            assert cost is not None and cost.state == "unknown" and cost.charged_micros == 3000
    finally:
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_l3_local_decoder_cannot_bypass_monetary_reservation(
    backend: str, tmp_path: Path
) -> None:
    from app.harness.budget import BudgetDenied

    recognizer = FakeStreamingRecognizer()
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer
    )
    try:
        session.privacy_level = PrivacyLevel.L3
        with pytest.raises(BudgetDenied, match="ephemeral_operation_run_forbidden"):
            await manager._start_streamer(session, b"\x01\x00" * 16)
        assert recognizer.fed_frames == 0 and not provider.requests
        async with storage.database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))
    finally:
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_unconsumed_decoder_has_no_provider_run_or_fee(backend: str, tmp_path: Path) -> None:
    recognizer = FakeStreamingRecognizer()
    storage, manager, session, service, _, _, _ = await fixture(backend, tmp_path, recognizer)
    try:
        context = await manager._ensure_capture_delivery(session, manager._source_claim(session))
        assert context is not None

        async def feed(pcm: bytes) -> str | None:
            raise AssertionError("unconsumed decoder was invoked")

        async def finalize() -> str | None:
            raise AssertionError("unconsumed decoder was finalized")

        request = context.recognition_request(recognizer, feed, finalize)
        await request.aclose()
        await manager.disconnect(session)
        async with storage.database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            assert len(runs) == 1 and runs[0].status == "cancelled"
            assert not list(await sql.scalars(select(ModelCostRecord)))
            assert not resource_usage(runs[0])
    finally:
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()
