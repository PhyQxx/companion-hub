"""Rebinding waits for cancelled prefetch; unstarted turns do not pin a session."""

import asyncio
from typing import Any

import pytest
from test_voice_budget_terminal import setup
from test_voice_websocket import FakeRecognizer

from app.schemas import PrivacyLevel
from app.voice.vad import EnergyVad


async def test_rebind_waits_for_cancelled_prefetch_cleanup_before_changing_scope() -> None:
    started, cleaning, release, closed = (asyncio.Event() for _ in range(4))

    class Recognizer(FakeRecognizer):
        async def transcribe(self, pcm: bytes, *, sample_rate: int, language: str | None) -> str:
            started.set()
            try:
                await asyncio.Event().wait()
                return "unused"
            finally:
                cleaning.set()
                await release.wait()
                closed.set()

    manager, session, socket = setup(Recognizer(""))
    session.vad = EnergyVad()
    session.utterance.extend(b"\x01\x00" * 16000)
    await manager._start_asr_prefetch(session)
    prefetch = session.asr_prefetch
    assert prefetch is not None
    await asyncio.wait_for(started.wait(), 1)
    task = asyncio.create_task(
        manager._on_hello(
            session,
            {
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
                "conversation_id": str(session.conversation_id),
                "privacy_level": "L0",
            },
        )
    )
    try:
        await asyncio.wait_for(cleaning.wait(), 1)
        assert not task.done() and session.privacy_level is PrivacyLevel.L1
        assert not any(frame["type"] == "voice.ready" for frame in socket.texts)
        release.set()
        await asyncio.wait_for(task, 1)
        assert closed.is_set() and prefetch.task.cancelled() and not session.asr_tasks
        assert str(session.privacy_level) == "L0" and socket.texts[-1]["type"] == "voice.ready"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.disconnect(session)


@pytest.mark.parametrize("entry", ["interrupt", "disconnect"])
async def test_cancel_before_turn_task_starts_releases_session_reference(entry: str) -> None:
    manager, session, socket = setup()
    entered = False

    async def turn() -> None:
        nonlocal entered
        entered = True
        await asyncio.Event().wait()

    task = asyncio.create_task(turn())
    session.turn_task = task
    if entry == "interrupt":
        await manager._interrupt(session, reason="client_interrupt")
    else:
        await manager.disconnect(session)
    assert not entered and task.cancelled() and session.turn_task is None
    assert session.generation_id is None
    assert not any(frame["type"] == "turn.accepted" for frame in socket.texts)


async def test_cancelled_queued_text_does_not_block_the_next_text_submission() -> None:
    from typing import cast

    from app.chat import ChatService
    from app.harness.budget import BudgetDenied

    class Chat:
        calls = 0

        async def start_turn(self, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            raise BudgetDenied("synthetic_stop")

    chat = Chat()
    manager, session, _ = setup()
    manager._service = cast(ChatService, chat)
    await manager._on_text_submit(session, {"text": "first synthetic submission"})
    await manager._interrupt(session, reason="client_interrupt")
    assert session.turn_task is None and chat.calls == 0
    await manager._on_text_submit(session, {"text": "second synthetic submission"})
    task = session.turn_task
    assert task is not None
    await task
    assert chat.calls == 1 and session.turn_task is None


async def test_interrupt_caller_cancellation_is_not_swallowed_during_turn_cleanup() -> None:
    manager, session, _ = setup()
    entered, cleaning = asyncio.Event(), asyncio.Event()

    async def turn() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await asyncio.Event().wait()

    child = asyncio.create_task(turn())
    session.turn_task = child
    await asyncio.wait_for(entered.wait(), 1)
    caller = asyncio.create_task(manager._interrupt(session, reason="client_interrupt"))
    try:
        await asyncio.wait_for(cleaning.wait(), 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert child.cancelled() and session.turn_task is None
    finally:
        child.cancel()
        await asyncio.gather(caller, child, return_exceptions=True)
