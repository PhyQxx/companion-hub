"""Streaming ASR denial must reach transport without a full-ASR retry."""

import json
from typing import cast

import pytest
from test_voice_ingress_limits import setup
from test_voice_streaming_privacy import FactoryRecognizer

from app.api.voice_ws import UtteranceStreamer
from app.harness.budget import BudgetDenied
from app.schemas import PrivacyLevel


@pytest.mark.parametrize("stage", ["feed", "finalize"])
@pytest.mark.parametrize("privacy", list(PrivacyLevel))
async def test_streaming_asr_denial_is_terminal(
    stage: str,
    privacy: PrivacyLevel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recognizer = FactoryRecognizer(parent_local=True, child_local=True)
    error = BudgetDenied("synthetic_streaming_budget_denied")

    def deny(*args: object) -> str:
        raise error

    monkeypatch.setattr(recognizer.child, stage, deny)
    streamer = UtteranceStreamer(recognizer, privacy_level=privacy)
    with pytest.raises(BudgetDenied) as captured:
        if stage == "feed":
            await streamer.feed(b"\x01\x00" * 32)
        else:
            await streamer.finalize()
    assert captured.value is error and not streamer.active


@pytest.mark.parametrize("stage", ["feed", "finalize"])
async def test_capture_denial_is_reported_and_cleared_without_closing_connection(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, session, socket, recognizer, _ = setup()
    error = BudgetDenied("synthetic_streaming_budget_denied")

    def deny(*args: object) -> str:
        raise error

    monkeypatch.setattr(recognizer.child, stage, deny)
    await manager._start_streamer(session, None)
    session.collecting = session.ptt_active = True
    session.utterance.extend(b"\x01\x00" * 16000)
    frames = iter(
        [
            {"type": "websocket.receive", "bytes": b"\x01\x00" * 32}
            if stage == "feed"
            else {"type": "websocket.receive", "text": json.dumps({"type": "utterance.end"})},
            {"type": "websocket.disconnect"},
        ]
    )

    async def receive() -> dict[str, object]:
        return cast(dict[str, object], next(frames))

    monkeypatch.setattr(socket, "receive", receive)
    try:
        await manager.run(session)
        assert [
            message["reason_code"] for message in socket.texts if message["type"] == "turn.failed"
        ] == [error.reason_code]
        assert not session.collecting and not session.ptt_active and not session.utterance
        assert session.utterance_streamer is None and session.turn_task is None
        assert not manager._sessions
    finally:
        await manager.disconnect(session)
