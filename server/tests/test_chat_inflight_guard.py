"""Chat source and stop fences work while a provider is silent, including opt-out."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select, update
from test_chat import _summary_service, create_user

from app.chat import TurnCancelled
from app.chat.context_sources import ContextSourceInvalidated
from app.config import DatabaseConfigStore, HubConfig
from app.db import (
    AppUserRecord,
    MemoryRecord,
    ModelCostRecord,
    ModelReservationRecord,
    TaskRunRecord,
)
from app.harness.budget import BudgetDenied
from app.llm import CompletionRequest, CompletionResult, LLMRoute, ModelUsage
from app.llm.router import LLMRouter
from app.memory import MemoryCandidate, MemoryIngester, MemoryStore, MemoryType
from app.runs.store import RunStore
from app.schemas import PrivacyLevel


class SilentProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return CompletionResult(
            text="synthetic response",
            provider="fixture",
            model="dialogue-v1",
            endpoint="cloud",
            route=LLMRoute.DIALOGUE,
            latency_ms=0,
            usage=ModelUsage(input_tokens=10, output_tokens=4, total_tokens=14, usage_known=True),
        )

    async def stream(
        self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
    ) -> CompletionResult:
        result = await self.complete(request)
        await on_delta(result.text)
        return result

    async def probe(self) -> None:
        raise AssertionError("offline fixture must not probe a provider")


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("change", ["cancel", "owner", "deadline", "memory"])
async def test_silent_chat_call_stops_on_persisted_revocation(
    tmp_path: Path,
    streaming: bool,
    enabled: bool,
    change: str,
) -> None:
    service, database, _ = await _summary_service(tmp_path)
    provider = SilentProvider()
    task: asyncio.Task[object] | None = None
    try:
        store = cast(DatabaseConfigStore, service._config_store)
        content = store.current.config.model_dump(mode="json")
        content["run_budget"]["enabled"] = enabled
        draft = await store.create_draft(HubConfig.model_validate(content), actor="offline")
        await store.publish(draft.version, actor="offline")
        service._router_builder = lambda config: LLMRouter(
            endpoints=config.models,
            routes=config.routes,
            providers={name: provider for name in config.models},
        )
        user = await create_user(database)
        if change == "memory":
            memory = MemoryStore(database)
            entry = await memory.add(
                MemoryCandidate(
                    type=MemoryType.PREFERENCE,
                    content="喜欢合成测试文本",
                    privacy_level=PrivacyLevel.L1,
                    pin=True,
                ),
                user_id=user.id,
            )
            service._memory_store = memory
            from app.memory import MemoryRetriever

            service._memory_retriever = MemoryRetriever(memory)
            service._memory_ingester = MemoryIngester(memory)
        conversation = await service.create_conversation(user_id=user.id, title="synthetic guard")
        if streaming:
            pending = await service.start_turn(
                conversation.id,
                user_id=user.id,
                text="合成测试文本",
                privacy_level=PrivacyLevel.L1,
            )

            async def delta(value: str) -> None:
                raise AssertionError("revoked fixture must not deliver a late delta")

            task = asyncio.create_task(service.run_stream(pending, delta))
        else:
            task = asyncio.create_task(
                service.send_message(
                    conversation.id,
                    user_id=user.id,
                    text="合成测试文本",
                    privacy_level=PrivacyLevel.L1,
                )
            )
        await asyncio.wait_for(provider.started.wait(), timeout=5)
        async with database.sessions() as session:
            run = await session.scalar(
                select(TaskRunRecord).where(TaskRunRecord.user_id == user.id)
            )
            assert run is not None
            run_id = run.id
        if change == "cancel":
            # Separate store simulates another worker, without local cancellation signals.
            assert (await RunStore(database).cancel_work(run_id, user_id=user.id))[0]
        else:
            async with database.sessions.begin() as session:
                if change == "owner":
                    await session.execute(
                        update(AppUserRecord)
                        .where(AppUserRecord.id == user.id)
                        .values(status="disabled")
                    )
                elif change == "deadline":
                    await session.execute(
                        update(TaskRunRecord)
                        .where(TaskRunRecord.id == run_id)
                        .values(deadline=datetime.now(UTC) - timedelta(seconds=1))
                    )
                else:
                    await session.execute(
                        update(MemoryRecord)
                        .where(MemoryRecord.id == entry.id)
                        .values(privacy_level="L2")
                    )
        expected = (
            BudgetDenied
            if change == "deadline"
            else ContextSourceInvalidated
            if change == "memory"
            else TurnCancelled
        )
        with pytest.raises(expected):
            await asyncio.wait_for(task, timeout=2)
        assert provider.cancelled.is_set()
        assert len(await service.list_messages(conversation.id, user_id=user.id)) == 1
        async with database.sessions() as session:
            saved = await session.get(TaskRunRecord, run_id)
            assert saved is not None and saved.status == (
                "cancelled" if change == "cancel" else "failed"
            )
            reservations = list(await session.scalars(select(ModelReservationRecord)))
            assert len(reservations) == 1 and reservations[0].state == "unknown"
            assert reservations[0].run_id == run_id
            fee = await session.get_one(ModelCostRecord, reservations[0].call_id)
            assert fee.state == "unknown" and fee.user_id == user.id
            if not enabled:
                assert saved.llm_attempts == saved.budget_tokens == 0
    finally:
        provider.release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.drain_background_work()
        await database.close()


async def test_cancellation_suppressing_stream_cannot_publish_after_revocation() -> None:
    from app.chat.budget import BudgetedBackend
    from app.ids import uuid7
    from app.llm import LLMMessage

    revoked = asyncio.Event()
    started = asyncio.Event()
    chunks: list[str] = []

    class Uncooperative(SilentProvider):
        async def stream(
            self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
        ) -> CompletionResult:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Simulate a transport that continues immediately after cancellation.
                await on_delta("late synthetic chunk")
                raise
            raise AssertionError("unreachable")

    async def validate() -> None:
        if revoked.is_set():
            raise TurnCancelled("generation_cancelled")

    async def delta(value: str) -> None:
        chunks.append(value)

    request = CompletionRequest(
        trace_id=uuid7(),
        messages=[LLMMessage(role="user", content="synthetic")],
        privacy_level=PrivacyLevel.L1,
        route=LLMRoute.DIALOGUE,
    )
    backend = BudgetedBackend(Uncooperative(), None, validate=validate)
    task = asyncio.create_task(backend.stream(request, delta))
    await asyncio.wait_for(started.wait(), timeout=2)
    revoked.set()
    with pytest.raises(TurnCancelled):
        await asyncio.wait_for(task, timeout=2)
    assert chunks == []
