"""Browser and device voice authority is checked before synthetic audio dispatch."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import delete, func, select, update
from test_run_cancel_fence import prepared
from test_voice_budget_terminal import AudioStream, Synthesizer, setup
from test_voice_streaming_privacy import FactoryRecognizer
from test_voice_websocket import FakeRecognizer

from app.api.voice_ws import VoiceWebSocketManager
from app.auth import ChatPrincipal
from app.chat import ChatService
from app.db import (
    AppUserRecord,
    AuthSessionRecord,
    ConversationRecord,
    DeviceClientRecord,
    TaskRunRecord,
)
from app.harness.budget import BudgetDenied
from app.harness.voice_sources import VoiceSourceClaim
from app.ids import uuid7
from app.runs.voice_sources import SqlVoiceSourceGuard
from app.schemas import PrivacyLevel
from app.voice.factory import StaticVoiceSource
from app.voice.failover import TtsProviderChain
from scripts.benchmark_storage import FixtureStorage


async def source_fixture(backend: str, tmp_path: Path) -> tuple[FixtureStorage, VoiceSourceClaim]:
    storage = await prepared(backend, tmp_path)
    now, user, conversation, actor = datetime.now(UTC), uuid7(), uuid7(), uuid7()
    try:
        async with storage.database.sessions.begin() as session:
            session.add(
                AppUserRecord(id=user, display_name="Synthetic voice owner", status="active")
            )
            await session.flush()
            session.add(ConversationRecord(id=conversation, user_id=user, title="Synthetic voice"))
            session.add(
                AuthSessionRecord(
                    id=actor,
                    user_id=user,
                    access_hash="a" * 64,
                    issued_at=now,
                    last_seen_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
        return storage, VoiceSourceClaim(user, conversation, "browser", actor, PrivacyLevel.L1)
    except BaseException:
        await storage.close()
        raise


async def invalidate(storage: FixtureStorage, source: VoiceSourceClaim, change: str) -> str:
    async with storage.database.sessions.begin() as session:
        if change in {"foreign", "archived", "deleted", "inactive"}:
            if change == "foreign":
                other = uuid7()
                session.add(AppUserRecord(id=other, display_name="Other fixture", status="active"))
                await session.flush()
                await session.execute(
                    update(ConversationRecord)
                    .where(ConversationRecord.id == source.conversation_id)
                    .values(user_id=other)
                )
            elif change == "archived":
                await session.execute(
                    update(ConversationRecord)
                    .where(ConversationRecord.id == source.conversation_id)
                    .values(status="archived")
                )
            elif change == "deleted":
                await session.execute(
                    delete(ConversationRecord).where(
                        ConversationRecord.id == source.conversation_id
                    )
                )
            else:
                await session.execute(
                    update(AppUserRecord)
                    .where(AppUserRecord.id == source.user_id)
                    .values(status="disabled")
                )
            return "voice_source_inactive"
        await session.execute(
            update(AuthSessionRecord)
            .where(AuthSessionRecord.id == source.actor_id)
            .values(
                {"revoked_at": datetime.now(UTC)}
                if change == "revoked"
                else {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
            )
        )
        return "voice_session_inactive"


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "change", ["foreign", "archived", "deleted", "inactive", "revoked", "expired"]
)
async def test_owned_source_guard_rejects_invalid_current_authority(
    backend: str, change: str, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    try:
        reason = await invalidate(storage, source, change)
        with pytest.raises(BudgetDenied, match=reason):
            await SqlVoiceSourceGuard(storage.database).validate(source)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("privacy", list(PrivacyLevel))
async def test_valid_browser_source_checks_only_authority_without_persistence(
    backend: str, privacy: PrivacyLevel, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    try:
        await SqlVoiceSourceGuard(storage.database).validate(replace(source, privacy_level=privacy))
        async with storage.database.sessions() as session:
            assert await session.scalar(select(func.count(TaskRunRecord.id))) == 0
            actor = await session.get_one(AuthSessionRecord, source.actor_id)
            assert actor.revoked_at is None
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "change", ["valid", "revoked", "missing_grant", "missing_capability", "foreign"]
)
async def test_satellite_actor_uses_device_grants_and_never_synthetic_browser_session(
    backend: str, change: str, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    try:
        device = uuid7()
        owner = source.user_id
        async with storage.database.sessions.begin() as session:
            if change == "foreign":
                owner = uuid7()
                session.add(AppUserRecord(id=owner, display_name="Other fixture", status="active"))
                await session.flush()
            session.add(
                DeviceClientRecord(
                    id=device,
                    owner_user_id=owner,
                    name="Synthetic satellite",
                    client_type="satellite",
                    credential_hash="b" * 64,
                    capabilities=[] if change == "missing_capability" else ["voice.satellite"],
                    granted_capabilities=[] if change == "missing_grant" else ["voice.satellite"],
                    paired_at=datetime.now(UTC),
                    last_seen_at=datetime.now(UTC),
                    revoked_at=datetime.now(UTC) if change == "revoked" else None,
                )
            )
        claim = replace(source, actor="satellite", actor_id=device)
        if change == "valid":
            await SqlVoiceSourceGuard(storage.database).validate(claim)
        else:
            with pytest.raises(BudgetDenied, match="voice_device_inactive"):
                await SqlVoiceSourceGuard(storage.database).validate(claim)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["foreign", "archived", "deleted", "revoked"])
async def test_asr_prefetch_never_dispatches_on_invalid_source(
    backend: str, change: str, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    try:
        await invalidate(storage, source, change)
        recognizer = FakeRecognizer("synthetic transcript")
        _, session, _ = setup()
        session.principal = ChatPrincipal(
            source.actor_id, source.user_id, "Synthetic", datetime.now(UTC) + timedelta(hours=1)
        )
        session.conversation_id = source.conversation_id
        session.utterance.extend(b"\x01\x00" * 16000)
        manager = VoiceWebSocketManager(
            cast(ChatService, object()),
            voice_source=StaticVoiceSource(recognizer, None),
            source_guard=SqlVoiceSourceGuard(storage.database),
        )
        try:
            await manager._start_asr_prefetch(session)
            if session.asr_prefetch is not None:
                await session.asr_prefetch.task
        except BudgetDenied:
            pass
        assert recognizer.calls == 0 and session.asr_prefetch is None
    finally:
        await storage.close()


def bind(session: object, source: VoiceSourceClaim) -> None:
    from app.api.voice_ws import VoiceSession

    value = cast(VoiceSession, session)
    value.principal = ChatPrincipal(
        source.actor_id, source.user_id, "Synthetic", datetime.now(UTC) + timedelta(hours=1)
    )
    value.conversation_id = source.conversation_id


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("stage", ["start", "feed", "finalize"])
async def test_streaming_asr_rejects_revoked_source_before_next_provider_frame(
    backend: str, stage: str, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    try:
        recognizer = FactoryRecognizer(parent_local=False, child_local=False)
        _, session, _ = setup()
        bind(session, source)
        manager = VoiceWebSocketManager(
            cast(ChatService, object()),
            voice_source=StaticVoiceSource(recognizer, None),
            source_guard=SqlVoiceSourceGuard(storage.database),
        )
        if stage != "start":
            await manager._start_streamer(session, None)
        await invalidate(storage, source, "revoked")
        with pytest.raises(BudgetDenied, match="voice_session_inactive"):
            if stage == "start":
                await manager._start_streamer(session, b"\x01\x00" * 32)
            elif stage == "feed":
                await manager._feed_streamer(session, b"\x01\x00" * 32)
            else:
                await manager._finalize_streamer(session)
        assert recognizer.child.fed_bytes == 0 and recognizer.transcribe_calls == 0
        assert recognizer.created == int(stage != "start")
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_revocation_while_cloud_asr_waits_drops_transcript_without_retry(
    backend: str, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    started, stopped = asyncio.Event(), asyncio.Event()

    class BlockingRecognizer(FakeRecognizer):
        runs_local = False

        async def transcribe(self, pcm: bytes, *, sample_rate: int, language: str | None) -> str:
            self.calls += 1
            started.set()
            try:
                await asyncio.Event().wait()
                return "synthetic late transcript"
            finally:
                stopped.set()

    recognizer = BlockingRecognizer("synthetic")
    _, session, socket = setup()
    bind(session, source)
    manager = VoiceWebSocketManager(
        cast(ChatService, object()),
        voice_source=StaticVoiceSource(recognizer, None),
        source_guard=SqlVoiceSourceGuard(storage.database),
    )
    task = asyncio.create_task(
        manager._run_utterance(session, b"\x01\x00" * 16000, recognizer, None)
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        await invalidate(storage, source, "revoked")
        await asyncio.wait_for(task, 2)
        assert recognizer.calls == 1 and stopped.is_set()
        assert socket.texts == [
            {"type": "turn.failed", "generation_id": None, "reason_code": "voice_session_inactive"}
        ]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_completed_prefetch_cannot_move_transcript_to_another_conversation(
    backend: str, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    try:
        recognizer = FakeRecognizer("synthetic transcript")
        _, session, _ = setup()
        bind(session, source)
        session.utterance.extend(b"\x01\x00" * 16000)
        manager = VoiceWebSocketManager(
            cast(ChatService, object()),
            voice_source=StaticVoiceSource(recognizer, None),
            source_guard=SqlVoiceSourceGuard(storage.database),
        )
        await manager._start_asr_prefetch(session)
        assert session.asr_prefetch is not None
        await session.asr_prefetch.task
        other = uuid7()
        async with storage.database.sessions.begin() as db_session:
            db_session.add(
                ConversationRecord(id=other, user_id=source.user_id, title="Other source")
            )
        session.conversation_id = other
        with pytest.raises(BudgetDenied, match="voice_source_changed"):
            await manager._consume_asr_prefetch(session, recognizer)
        assert recognizer.calls == 1 and session.asr_prefetch is None
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_foreign_hello_does_not_rebind_session_or_emit_ready(
    backend: str, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    try:
        _, session, socket = setup()
        bind(session, source)
        await invalidate(storage, source, "foreign")
        manager = VoiceWebSocketManager(
            cast(ChatService, object()),
            voice_source=StaticVoiceSource(None, None),
            source_guard=SqlVoiceSourceGuard(storage.database),
        )
        with pytest.raises(BudgetDenied, match="voice_source_inactive"):
            await manager._on_hello(
                session,
                {
                    "conversation_id": str(source.conversation_id),
                    "privacy_level": "L2",
                    "format": "pcm_s16le",
                    "sample_rate": 16000,
                    "channels": 1,
                },
            )
        assert session.privacy_level == PrivacyLevel.L1 and not socket.texts
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_proactive_tts_rejects_invalid_recipient_before_text_or_synthesis(
    backend: str, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    try:
        first, backup = Synthesizer(AudioStream(None)), Synthesizer(AudioStream(None))
        chain = TtsProviderChain([first, backup])
        _, session, socket = setup()
        bind(session, source)
        manager = VoiceWebSocketManager(
            cast(ChatService, object()),
            voice_source=StaticVoiceSource(None, chain),
            source_guard=SqlVoiceSourceGuard(storage.database),
        )
        await invalidate(storage, source, "revoked")
        with pytest.raises(BudgetDenied, match="voice_session_inactive"):
            await manager._send_proactive(session, "synthetic text", PrivacyLevel.L1)
        assert first.calls == backup.calls == 0 and not socket.texts and not socket.audio
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_revocation_during_tts_wait_closes_stream_without_fallback_or_end(
    backend: str, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    waiting = asyncio.Event()

    class WaitingAudio(AudioStream):
        async def __anext__(self) -> bytes:
            if self.reads == 0:
                return await super().__anext__()
            waiting.set()
            await asyncio.Event().wait()
            raise StopAsyncIteration

    first, backup = Synthesizer(WaitingAudio(None)), Synthesizer(AudioStream(None))
    chain = TtsProviderChain([first, backup])
    _, session, socket = setup()
    bind(session, source)
    manager = VoiceWebSocketManager(
        cast(ChatService, object()),
        voice_source=StaticVoiceSource(None, chain),
        source_guard=SqlVoiceSourceGuard(storage.database),
    )
    task = asyncio.create_task(manager._send_proactive(session, "synthetic text", PrivacyLevel.L1))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        await invalidate(storage, source, "revoked")
        with pytest.raises(BudgetDenied, match="voice_session_inactive"):
            await asyncio.wait_for(task, 2)
        assert first.calls == first.stream.closed == 1 and backup.calls == 0
        assert len(socket.audio) == 1 and not chain._blocked_until
        assert not any(frame["type"] == "voice.sentence.end" for frame in socket.texts)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_queued_transcript_cannot_bind_to_a_new_source_before_turn_start(
    backend: str, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    try:
        _, session, socket = setup()
        bind(session, source)
        session.conversation_id = uuid7()
        recognizer = FakeRecognizer("synthetic transcript")
        manager = VoiceWebSocketManager(
            cast(ChatService, object()),
            voice_source=StaticVoiceSource(recognizer, None),
            source_guard=SqlVoiceSourceGuard(storage.database),
        )
        await manager._run_utterance(
            session,
            b"",
            recognizer,
            None,
            prefetched_transcript="synthetic transcript",
            source_claim=source,
        )
        assert recognizer.calls == 0 and session.last_transcript is None
        assert socket.texts == [
            {"type": "turn.failed", "generation_id": None, "reason_code": "voice_source_changed"}
        ]
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_session_expiry_is_rechecked_after_database_wait(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.selectable import Select

    from app.runs import voice_sources

    storage, source = await source_fixture(backend, tmp_path)
    before = datetime.now(UTC)
    now = before
    original = AsyncSession.scalar

    class Clock:
        @staticmethod
        def now(tz: object = None) -> datetime:
            return now

    async def waited(session: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal now
        result = await original(session, statement, *args, **kwargs)
        if isinstance(statement, Select) and list(statement.selected_columns.keys()) == [
            "expires_at"
        ]:
            now = before + timedelta(seconds=2)
        return result

    try:
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(AuthSessionRecord)
                .where(AuthSessionRecord.id == source.actor_id)
                .values(expires_at=before + timedelta(seconds=1))
            )
        monkeypatch.setattr(voice_sources, "datetime", Clock)
        monkeypatch.setattr(AsyncSession, "scalar", waited)
        with pytest.raises(BudgetDenied, match="voice_session_inactive"):
            await SqlVoiceSourceGuard(storage.database).validate(source)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_revocation_during_actual_chat_run_preserves_unknown_model_fee_and_no_reply(
    backend: str, tmp_path: Path
) -> None:
    from collections.abc import Awaitable, Callable

    from test_llm import FakeProvider
    from test_voice_websocket import config_yaml

    from app.config import DatabaseConfigStore
    from app.db import MessageRecord, ModelCostRecord
    from app.llm import CompletionRequest, CompletionResult, LLMRouter

    storage, source = await source_fixture(backend, tmp_path)
    started, stopped = asyncio.Event(), asyncio.Event()

    class WaitingProvider(FakeProvider):
        async def stream(
            self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
        ) -> CompletionResult:
            self.requests.append(request)
            started.set()
            try:
                await asyncio.Event().wait()
                raise AssertionError("synthetic model cannot finish")
            finally:
                stopped.set()

    provider = WaitingProvider("cloud")
    backup = FakeProvider("local")
    path = tmp_path / "source-owned-voice.yaml"
    path.write_text(
        config_yaml()
        .replace("input_cost_per_million: 0", "input_cost_per_million: 1")
        .replace("output_cost_per_million: 0", "output_cost_per_million: 2\n    cost_currency: CNY")
    )
    config = DatabaseConfigStore(storage.database, path)
    await config.load()
    chat = ChatService(
        storage.database,
        config,
        router_builder=lambda value: LLMRouter(
            endpoints=value.models,
            routes=value.routes,
            providers={"cloud": provider, "local": backup},
        ),
    )
    recognizer = FakeRecognizer("synthetic utterance")
    _, session, socket = setup()
    bind(session, source)
    manager = VoiceWebSocketManager(
        chat,
        voice_source=StaticVoiceSource(recognizer, None),
        source_guard=SqlVoiceSourceGuard(storage.database),
    )
    task = asyncio.create_task(
        manager._run_utterance(session, b"\x01\x00" * 16000, recognizer, None)
    )
    try:
        await asyncio.wait_for(started.wait(), 3)
        await invalidate(storage, source, "revoked")
        await asyncio.wait_for(task, 3)
        assert len(provider.requests) == 1 and not backup.requests and stopped.is_set()
        assert any(frame.get("reason_code") == "voice_session_inactive" for frame in socket.texts)
        assert not any(frame["type"] == "reply.committed" for frame in socket.texts)
        async with storage.database.sessions() as db_session:
            root = (await db_session.scalars(select(TaskRunRecord))).one()
            fee = (await db_session.scalars(select(ModelCostRecord))).one()
            assert root.status in {"failed", "cancelled"} and root.llm_attempts == 1
            assert fee.state == "unknown" and fee.charged_micros == fee.reserved_micros
            assert fee.charged_micros is not None and fee.charged_micros > 0
            assert (
                await db_session.scalar(
                    select(func.count(MessageRecord.id)).where(MessageRecord.role == "assistant")
                )
                == 0
            )
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_source_validation_joins_database_read_after_repeated_cancellation(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from sqlalchemy.ext.asyncio import AsyncSession

    storage, source = await source_fixture(backend, tmp_path)
    acquired, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    scalar = AsyncSession.scalar

    async def waiting(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        result = await scalar(self, *args, **kwargs)
        acquired.set()
        try:
            await release.wait()
        finally:
            finished.set()
        return result

    monkeypatch.setattr(AsyncSession, "scalar", waiting)
    task = asyncio.create_task(SqlVoiceSourceGuard(storage.database).validate(source))
    try:
        await asyncio.wait_for(acquired.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert storage.database.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()
