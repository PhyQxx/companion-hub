"""A delayed actor check must not revive an earlier delivery authority read."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from test_speech_delivery import fixture as device_fixture
from test_voice_budget_terminal import AudioStream, Synthesizer
from test_voice_turn_delivery import fixture as turn_fixture
from test_voice_websocket import FakeRecognizer

from app.db import AppUserRecord, AuthSessionRecord, DeviceClientRecord, TaskRunRecord
from app.harness.budget import BudgetDenied
from app.harness.voice_sources import VoiceAuthority, VoiceRecipientClaim
from app.ids import uuid7
from app.runs.speech_delivery import SqlSpeechDelivery, _Delivery
from app.runs.voice_sources import SqlVoiceSourceGuard
from app.runs.voice_turn import SqlVoiceTurnDelivery


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("kind", ["device", "turn"])
@pytest.mark.parametrize(
    "change",
    [
        "cancelled",
        "cancel_requested",
        "expired",
        "overflow",
        "privacy",
        "missing_deadline",
        "budget_disabled",
    ],
)
async def test_root_changes_while_final_actor_check_waits_are_rejected(
    backend: str, kind: str, change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = session = None
    if kind == "turn":
        storage, manager, session, service, _, _, _ = await turn_fixture(
            backend, tmp_path, FakeRecognizer("unused")
        )
        port = cast(SqlVoiceTurnDelivery, manager._voice_turn_delivery).speech
        source: VoiceAuthority = manager._source_claim(session)
    else:
        storage, source, manager, _, _ = await device_fixture(
            backend, tmp_path, [Synthesizer(AudioStream(None))]
        )
        port = cast(SqlSpeechDelivery, manager._speech_delivery)
    snapshot = port.config.current
    context = _Delivery(port, source, uuid7(), snapshot.version, snapshot.config.run_budget, None)
    entered, release = asyncio.Event(), asyncio.Event()
    guard = cast(SqlVoiceSourceGuard, port.source_guard)
    original = guard.validate

    async def validate(claim: VoiceAuthority) -> None:
        entered.set()
        await release.wait()
        await original(claim)

    task = None
    try:
        await original(source)
        await context.create()
        monkeypatch.setattr(guard, "validate", validate)
        task = asyncio.create_task(context.validate())
        await asyncio.wait_for(entered.wait(), 3)
        async with storage.database.sessions.begin() as sql:
            row = await sql.get_one(TaskRunRecord, context.run_id)
            if change == "cancelled":
                row.status = "cancelled"
            elif change == "expired":
                row.deadline = datetime.now(UTC) - timedelta(seconds=1)
            elif change == "privacy":
                row.privacy_level = "L2"
            elif change == "missing_deadline":
                row.deadline = None
            elif change == "budget_disabled":
                row.budget = {**(row.budget or {}), "enabled": False}
            else:
                row.contract = {
                    **row.contract,
                    "work_cancel_requested"
                    if change == "cancel_requested"
                    else "budget_usage_overflow": True,
                }
        release.set()
        with pytest.raises(BudgetDenied):
            await asyncio.wait_for(task, 3)
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await context.finish("cancelled", "synthetic_cleanup")
        if service is not None:
            await service.drain_background_work()
        if session is not None:
            await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("kind", ["device", "turn"])
@pytest.mark.parametrize("change", ["actor_revoked", "owner_disabled"])
async def test_final_joint_query_rechecks_actor_after_source_read(
    backend: str, kind: str, change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = session = None
    if kind == "turn":
        storage, manager, session, service, _, _, _ = await turn_fixture(
            backend, tmp_path, FakeRecognizer("unused")
        )
        port = cast(SqlVoiceTurnDelivery, manager._voice_turn_delivery).speech
        source: VoiceAuthority = manager._source_claim(session)
    else:
        storage, source, manager, _, _ = await device_fixture(
            backend, tmp_path, [Synthesizer(AudioStream(None))]
        )
        port = cast(SqlSpeechDelivery, manager._speech_delivery)
    snapshot = port.config.current
    context = _Delivery(port, source, uuid7(), snapshot.version, snapshot.config.run_budget, None)
    entered, release = asyncio.Event(), asyncio.Event()
    original = port.source_guard.validate

    async def validate(claim: VoiceAuthority) -> None:
        await original(claim)
        entered.set()
        await release.wait()

    task = None
    try:
        await context.create()
        monkeypatch.setattr(port.source_guard, "validate", validate)
        task = asyncio.create_task(context.validate())
        await asyncio.wait_for(entered.wait(), 3)
        async with storage.database.sessions.begin() as sql:
            if change == "owner_disabled":
                owner = await sql.get_one(AppUserRecord, source.user_id)
                owner.status = "disabled"
            elif isinstance(source, VoiceRecipientClaim):
                device = await sql.get_one(DeviceClientRecord, source.device_id)
                device.revoked_at = datetime.now(UTC)
            else:
                actor = await sql.get_one(AuthSessionRecord, source.actor_id)
                actor.revoked_at = datetime.now(UTC)
        release.set()
        with pytest.raises(BudgetDenied, match="budget_run_inactive"):
            await asyncio.wait_for(task, 3)
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await context.finish("cancelled", "synthetic_cleanup")
        if service is not None:
            await service.drain_background_work()
        if session is not None:
            await manager.disconnect(session)
        await storage.close()
