"""Actual owned device authority protects synthetic TTS and broadcast text."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from fastapi import WebSocket
from sqlalchemy import delete, func, select, update
from test_voice_budget_terminal import AudioStream, Synthesizer
from test_voice_source_guard import source_fixture

from app.api.device_commands import DeviceCommandConnection, DeviceCommandGateway
from app.api.voice_ws import VoiceWebSocketManager
from app.chat import ChatService
from app.db import AppUserRecord, DeviceClientRecord, TaskRunRecord
from app.devices import DeviceCommandStore, DevicePrincipal, DeviceRegistry
from app.harness.budget import BudgetDenied
from app.harness.voice_sources import VoiceRecipientClaim
from app.ids import uuid7
from app.runs.voice_sources import SqlVoiceSourceGuard
from app.satellite import SatelliteState
from app.schemas import PrivacyLevel
from app.voice.factory import StaticVoiceSource
from app.voice.failover import TtsProviderChain
from scripts.benchmark_storage import FixtureStorage

Capability = Literal["avatar.chat", "voice.satellite"]


async def fixture(
    backend: str, tmp_path: Path, capability: Capability
) -> tuple[FixtureStorage, VoiceRecipientClaim]:
    storage, source = await source_fixture(backend, tmp_path)
    recipient = VoiceRecipientClaim(source.user_id, uuid7(), capability, PrivacyLevel.L1)
    try:
        async with storage.database.sessions.begin() as session:
            session.add(
                DeviceClientRecord(
                    id=recipient.device_id,
                    owner_user_id=recipient.user_id,
                    name="Synthetic speaker",
                    client_type="satellite" if capability == "voice.satellite" else "desktop",
                    credential_hash="c" * 64,
                    capabilities=[capability],
                    granted_capabilities=[capability],
                    paired_at=datetime.now(UTC),
                    last_seen_at=datetime.now(UTC),
                )
            )
        return storage, recipient
    except BaseException:
        await storage.close()
        raise


async def revoke(storage: FixtureStorage, recipient: VoiceRecipientClaim) -> None:
    async with storage.database.sessions.begin() as session:
        await session.execute(
            update(DeviceClientRecord)
            .where(DeviceClientRecord.id == recipient.device_id)
            .values(revoked_at=datetime.now(UTC))
        )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("capability", ["avatar.chat", "voice.satellite"])
@pytest.mark.parametrize(
    "change",
    [
        "valid",
        "revoked",
        "foreign",
        "missing_grant",
        "missing_capability",
        "deleted",
        "inactive_owner",
    ],
)
async def test_recipient_requires_owned_active_user_and_current_device_grant(
    backend: str, capability: Capability, change: str, tmp_path: Path
) -> None:
    storage, recipient = await fixture(backend, tmp_path, capability)
    try:
        async with storage.database.sessions.begin() as session:
            if change == "revoked":
                await session.execute(
                    update(DeviceClientRecord)
                    .where(DeviceClientRecord.id == recipient.device_id)
                    .values(revoked_at=datetime.now(UTC))
                )
            elif change == "foreign":
                foreign = uuid7()
                session.add(AppUserRecord(id=foreign, display_name="Other", status="active"))
                await session.flush()
                await session.execute(
                    update(DeviceClientRecord)
                    .where(DeviceClientRecord.id == recipient.device_id)
                    .values(owner_user_id=foreign)
                )
            elif change in {"missing_grant", "missing_capability"}:
                await session.execute(
                    update(DeviceClientRecord)
                    .where(DeviceClientRecord.id == recipient.device_id)
                    .values(
                        {"granted_capabilities": []}
                        if change == "missing_grant"
                        else {"capabilities": []}
                    )
                )
            elif change == "deleted":
                await session.execute(
                    delete(DeviceClientRecord).where(DeviceClientRecord.id == recipient.device_id)
                )
            elif change == "inactive_owner":
                await session.execute(
                    update(AppUserRecord)
                    .where(AppUserRecord.id == recipient.user_id)
                    .values(status="disabled")
                )
        guard = SqlVoiceSourceGuard(storage.database)
        if change == "valid":
            await guard.validate(recipient)
            async with storage.database.sessions() as session:
                assert await session.scalar(select(func.count(TaskRunRecord.id))) == 0
        else:
            reason = (
                "voice_recipient_inactive"
                if change == "inactive_owner"
                else "voice_device_inactive"
            )
            with pytest.raises(BudgetDenied, match=reason):
                await guard.validate(recipient)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("capability", ["avatar.chat", "voice.satellite"])
async def test_revoked_recipient_never_resolves_or_calls_tts(
    backend: str, capability: Capability, tmp_path: Path
) -> None:
    storage, recipient = await fixture(backend, tmp_path, capability)
    provider = Synthesizer(AudioStream(None))
    backup = Synthesizer(AudioStream(None))
    chain = TtsProviderChain([provider, backup])
    emitted: list[tuple[str, dict[str, Any]]] = []

    class Source(StaticVoiceSource):
        resolves = 0

        async def resolve(self) -> tuple[None, TtsProviderChain]:
            self.resolves += 1
            return None, chain

    source = Source(None, chain)

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        emitted.append((kind, payload))

    manager = VoiceWebSocketManager(
        cast(ChatService, object()),
        voice_source=source,
        source_guard=SqlVoiceSourceGuard(storage.database),
    )
    try:
        await revoke(storage, recipient)
        with pytest.raises(BudgetDenied, match="voice_device_inactive"):
            await manager.stream_device_speech(recipient, "synthetic text", emit)
        assert source.resolves == provider.calls == backup.calls == 0
        assert not chain._blocked_until
        assert emitted == [("pet.audio.failed", {"reason_code": "voice_device_inactive"})]
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("capability", ["avatar.chat", "voice.satellite"])
@pytest.mark.parametrize("after_chunk", [False, True])
async def test_revoked_recipient_stops_waiting_tts_and_closes_stream_without_fallback(
    backend: str, capability: Capability, after_chunk: bool, tmp_path: Path
) -> None:
    storage, recipient = await fixture(backend, tmp_path, capability)
    waiting, closed = asyncio.Event(), asyncio.Event()

    class WaitingStream(AudioStream):
        async def __anext__(self) -> bytes:
            self.reads += 1
            if after_chunk and self.reads == 1:
                return b"\x01\x00"
            waiting.set()
            await asyncio.Event().wait()
            raise AssertionError("synthetic stream cannot complete")

        async def aclose(self) -> None:
            self.closed += 1
            closed.set()

    provider = Synthesizer(WaitingStream(None))
    backup = Synthesizer(AudioStream(None))
    chain = TtsProviderChain([provider, backup])
    emitted: list[str] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        emitted.append(kind)

    manager = VoiceWebSocketManager(
        cast(ChatService, object()),
        voice_source=StaticVoiceSource(None, chain),
        source_guard=SqlVoiceSourceGuard(storage.database),
    )
    task = asyncio.create_task(manager.stream_device_speech(recipient, "synthetic text", emit))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        await revoke(storage, recipient)
        with pytest.raises(BudgetDenied, match="voice_device_inactive"):
            await asyncio.wait_for(task, 2)
        assert provider.calls == provider.stream.closed == 1 and closed.is_set()
        assert backup.calls == 0 and not chain._blocked_until
        assert emitted.count("pet.audio.chunk") == int(after_chunk)
        assert emitted[-1] == "pet.audio.failed" and "pet.audio.end" not in emitted
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("stage", ["before", "after_state"])
async def test_satellite_broadcast_rejects_revoked_device_before_text_or_tts(
    backend: str, stage: str, tmp_path: Path
) -> None:
    storage, recipient = await fixture(backend, tmp_path, "voice.satellite")
    frames: list[dict[str, Any]] = []
    calls = 0

    class Socket:
        async def send_json(self, frame: dict[str, Any]) -> None:
            frames.append(frame)
            if stage == "after_state" and frame.get("state") == "speaking":
                await revoke(storage, recipient)

    gateway = DeviceCommandGateway(
        DeviceRegistry(storage.database), DeviceCommandStore(storage.database)
    )
    connection = DeviceCommandConnection(
        cast(WebSocket, Socket()),
        DevicePrincipal(recipient.device_id, recipient.user_id),
        "synthetic-signed-device-token",
        capabilities=("voice.satellite",),
    )
    gateway._connections[recipient.device_id] = connection
    gateway.satellites.register(
        device_id=recipient.device_id, owner_user_id=recipient.user_id, room_id="fixture"
    )

    async def speak(claim: VoiceRecipientClaim, text: str, emit: Any) -> bool:
        nonlocal calls
        calls += 1
        return True

    gateway.set_pet_audio_handler(speak, source_guard=SqlVoiceSourceGuard(storage.database))
    try:
        if stage == "before":
            await revoke(storage, recipient)
        assert (
            await gateway.broadcast_satellite(
                recipient.user_id, "private synthetic broadcast", privacy_level=PrivacyLevel.L1
            )
            == 0
        )
        assert calls == 0 and not any(frame["type"] == "satellite.broadcast" for frame in frames)
        state = gateway.satellites.get(recipient.device_id)
        assert state is not None and state.state is SatelliteState.IDLE
    finally:
        gateway.disconnect(connection)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_gateway_preserves_recipient_identity_and_emits_budget_failure_once(
    backend: str, tmp_path: Path
) -> None:
    storage, recipient = await fixture(backend, tmp_path, "avatar.chat")
    frames: list[dict[str, Any]] = []
    provider = Synthesizer(AudioStream(BudgetDenied("synthetic_fee_denied")))
    manager = VoiceWebSocketManager(
        cast(ChatService, object()),
        voice_source=StaticVoiceSource(None, TtsProviderChain([provider])),
        source_guard=SqlVoiceSourceGuard(storage.database),
    )

    class Socket:
        async def send_json(self, frame: dict[str, Any]) -> None:
            frames.append(frame)

    connection = DeviceCommandConnection(
        cast(WebSocket, Socket()),
        DevicePrincipal(recipient.device_id, recipient.user_id),
        "synthetic-signed-device-token",
        capabilities=("avatar.chat",),
    )
    gateway = DeviceCommandGateway(
        DeviceRegistry(storage.database), DeviceCommandStore(storage.database)
    )
    gateway.set_pet_audio_handler(
        manager.stream_device_speech, source_guard=SqlVoiceSourceGuard(storage.database)
    )
    try:
        assert not await gateway._stream_pet_audio(
            connection, uuid7(), "synthetic text", PrivacyLevel.L1
        )
        assert provider.calls == 1 and provider.stream.closed == 1
        assert len(frames) == 1 and frames[0]["type"] == "pet.audio.failed"
        assert frames[0]["reason_code"] == "synthetic_fee_denied"
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "actor,change",
    [
        ("browser", "owner"),
        ("satellite", "owner"),
        ("recipient", "owner"),
        ("browser", "conversation"),
        ("satellite", "conversation"),
    ],
)
async def test_final_actor_lookup_rechecks_authority_after_prior_source_read(
    backend: str, actor: str, change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db import AuthSessionRecord, ConversationRecord
    from app.harness.voice_sources import VoiceAuthority, VoiceSourceClaim

    storage, recipient = await fixture(backend, tmp_path, "voice.satellite")
    captured, release = asyncio.Event(), asyncio.Event()
    scalar = AsyncSession.scalar
    blocked = False
    task: asyncio.Task[None] | None = None
    try:
        async with storage.database.sessions() as session:
            conversation = (await session.scalars(select(ConversationRecord.id))).one()
            browser = (await session.scalars(select(AuthSessionRecord.id))).one()
        authority: VoiceAuthority = (
            recipient
            if actor == "recipient"
            else VoiceSourceClaim(
                recipient.user_id,
                conversation,
                "browser" if actor == "browser" else "satellite",
                browser if actor == "browser" else recipient.device_id,
                PrivacyLevel.L1,
            )
        )

        async def paused(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
            nonlocal blocked
            result = await scalar(self, *args, **kwargs)
            if not blocked:
                blocked = True
                captured.set()
                await release.wait()
            return result

        monkeypatch.setattr(AsyncSession, "scalar", paused)
        task = asyncio.create_task(SqlVoiceSourceGuard(storage.database).validate(authority))
        await asyncio.wait_for(captured.wait(), 1)
        async with storage.database.sessions.begin() as session:
            if change == "owner":
                await session.execute(
                    update(AppUserRecord)
                    .where(AppUserRecord.id == recipient.user_id)
                    .values(status="disabled")
                )
            else:
                await session.execute(
                    update(ConversationRecord)
                    .where(ConversationRecord.id == conversation)
                    .values(status="archived")
                )
        release.set()
        with pytest.raises(BudgetDenied):
            await task
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await storage.close()
