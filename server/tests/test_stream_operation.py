"""A synthetic lazy stream retains its Run, quote and parent until provider cleanup."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, update
from test_resource_budget import seed
from test_run_cancel_fence import prepared

from app.config.models import RunBudgetConfig
from app.db import ModelCostRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, budget_scope
from app.harness.operations import OperationPolicy
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.runs.budget import RunModelBudget
from app.runs.stream_operation import stream_with_run
from app.schemas import PrivacyLevel
from scripts.benchmark_storage import FixtureStorage


class Stream:
    def __init__(self, *, wait: bool = False, error: BaseException | None = None) -> None:
        self.wait, self.error = wait, error
        self.reads = self.closed = 0
        self.waiting, self.release = asyncio.Event(), asyncio.Event()
        self.close_started, self.close_release = asyncio.Event(), asyncio.Event()
        self.close_release.set()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        self.reads += 1
        if self.reads == 1:
            return b"\x01\x00"
        if self.wait:
            self.waiting.set()
            await self.release.wait()
        if self.error is not None:
            raise self.error
        if self.reads == 2:
            return b"\x02\x00"
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_started.set()
        await self.close_release.wait()
        self.closed += 1


async def fixture(
    backend: str, tmp_path: Path
) -> tuple[FixtureStorage, UUID, UUID, OperationPolicy]:
    storage = await prepared(backend, tmp_path)
    try:
        owner, parent, _ = await seed(storage.database)
        config = RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.01)
        quote = UnitCostQuote(
            pricing=UnitPricing(unit="request", currency="CNY", rate_per_unit=Decimal("0.005")),
            maximum_quantity=Decimal(1),
        )
    except BaseException:
        await storage.close()
        raise
    return (
        storage,
        owner,
        parent,
        OperationPolicy(1, tuple(config.model_dump(mode="json").items()), quote),
    )


async def guard() -> None:
    return None


def execute(
    storage: FixtureStorage,
    owner: UUID,
    policy: OperationPolicy,
    create: Callable[[], AsyncIterator[bytes]],
    validate: Callable[[], Awaitable[None]] = guard,
) -> AsyncGenerator[bytes, None]:
    return stream_with_run(
        storage.database,
        policy,
        user_id=owner,
        privacy_level=PrivacyLevel.L1,
        entry="synthetic.stream",
        create_stream=create,
        evidence=lambda: {"transport_evidence": "synthetic_stream_exhausted"},
        source_guard=validate,
        cost_endpoint="synthetic-tts",
    )


async def rows(storage: FixtureStorage) -> tuple[TaskRunRecord, ModelCostRecord]:
    async with storage.database.sessions() as session:
        run = (
            await session.scalars(
                select(TaskRunRecord).where(
                    TaskRunRecord.contract["entry"].as_string() == "synthetic.stream"
                )
            )
        ).one()
        fee = (await session.scalars(select(ModelCostRecord))).one()
        return run, fee


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_exhaustion_closes_provider_once_and_records_unknown_fee_without_body(
    backend: str, tmp_path: Path
) -> None:
    storage, owner, _, policy = await fixture(backend, tmp_path)
    provider = Stream()
    try:
        assert [chunk async for chunk in execute(storage, owner, policy, lambda: provider)] == [
            b"\x01\x00",
            b"\x02\x00",
        ]
        run, fee = await rows(storage)
        assert (
            run.status == "succeeded" and run.contract["criterion"] == "provider_response_returned"
        )
        assert run.contract["transport_evidence"] == "synthetic_stream_exhausted"
        assert fee.state == "unknown" and fee.charged_micros == 5000 and fee.unit_quantity is None
        assert provider.closed == 1 and "synthetic text" not in str(run.contract)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_close_waits_for_provider_cleanup_and_terminal_sql_before_return(
    backend: str, tmp_path: Path
) -> None:
    storage, owner, _, policy = await fixture(backend, tmp_path)
    provider = Stream(wait=True)
    provider.close_release.clear()
    stream = execute(storage, owner, policy, lambda: provider)
    try:
        assert await anext(stream) == b"\x01\x00"
        await asyncio.wait_for(provider.waiting.wait(), 1)
        run, fee = await rows(storage)
        assert run.status == "running" and fee.state == "unknown"
        closing = asyncio.create_task(stream.aclose())
        await asyncio.wait_for(provider.close_started.wait(), 1)
        assert not closing.done()
        provider.close_release.set()
        await asyncio.wait_for(closing, 2)
        run, fee = await rows(storage)
        assert run.status == "cancelled" and run.contract["operation_state"] == "unknown"
        assert fee.charged_micros == 5000 and provider.closed == 1
    finally:
        provider.release.set()
        provider.close_release.set()
        await stream.aclose()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("terminal", ["budget", "ordinary"])
async def test_midstream_terminal_error_preserves_identity_and_never_replays(
    backend: str, terminal: str, tmp_path: Path
) -> None:
    storage, owner, _, policy = await fixture(backend, tmp_path)
    error = (
        BudgetDenied("synthetic_stream_denied")
        if terminal == "budget"
        else RuntimeError("synthetic_stream_failed")
    )
    provider, calls = Stream(error=error), 0

    def create() -> Stream:
        nonlocal calls
        calls += 1
        return provider

    try:
        stream = execute(storage, owner, policy, create)
        assert await anext(stream) == b"\x01\x00"
        with pytest.raises(type(error)) as caught:
            await anext(stream)
        assert caught.value is error and calls == provider.closed == 1
        run, fee = await rows(storage)
        assert run.status == "failed" and fee.charged_micros == 5000 and fee.state == "unknown"
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_lazy_never_consumed_stream_does_not_dispatch_or_create_fee(
    backend: str, tmp_path: Path
) -> None:
    storage, owner, _, policy = await fixture(backend, tmp_path)
    provider = Stream()
    stream = execute(storage, owner, policy, lambda: provider)
    try:
        await stream.aclose()
        async with storage.database.sessions() as session:
            assert list(await session.scalars(select(ModelCostRecord))) == []
            assert len(list(await session.scalars(select(TaskRunRecord)))) == 1
        assert provider.reads == provider.closed == 0
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_invalid_parent_never_constructs_provider_or_reserves_child_fee(
    backend: str, tmp_path: Path
) -> None:
    storage, owner, root, policy = await fixture(backend, tmp_path)
    calls = 0

    def create() -> Stream:
        nonlocal calls
        calls += 1
        return Stream()

    try:
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == root)
                .values(status="cancelled", updated_at=datetime.now(UTC))
            )
        config = RunBudgetConfig.model_validate(dict(policy.budget))
        with budget_scope(
            RunModelBudget(storage.database, run_id=root, user_id=owner, config=config)
        ):
            stream = execute(storage, owner, policy, create)
            with pytest.raises(BudgetDenied, match="budget_run_inactive"):
                await anext(stream)
        assert calls == 0
        async with storage.database.sessions() as session:
            assert list(await session.scalars(select(ModelCostRecord))) == []
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_source_revocation_while_provider_waits_closes_without_new_chunk(
    backend: str, tmp_path: Path
) -> None:
    storage, owner, _, policy = await fixture(backend, tmp_path)
    provider, revoked = Stream(wait=True), False
    error = BudgetDenied("synthetic_stream_source_revoked")

    async def validate() -> None:
        if revoked:
            raise error

    stream = execute(storage, owner, policy, lambda: provider, validate)
    try:
        assert await anext(stream) == b"\x01\x00"
        await asyncio.wait_for(provider.waiting.wait(), 1)
        revoked = True
        with pytest.raises(BudgetDenied) as caught:
            await asyncio.wait_for(anext(stream), 2)
        assert caught.value is error and provider.closed == 1
        run, fee = await rows(storage)
        assert run.status == "failed" and fee.state == "unknown" and fee.charged_micros == 5000
    finally:
        provider.release.set()
        await stream.aclose()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_repeated_close_cancellation_does_not_cancel_provider_or_sql_cleanup_again(
    backend: str, tmp_path: Path
) -> None:
    storage, owner, _, policy = await fixture(backend, tmp_path)
    provider = Stream(wait=True)
    provider.close_release.clear()
    stream = execute(storage, owner, policy, lambda: provider)
    closing: asyncio.Task[None] | None = None
    try:
        assert await anext(stream) == b"\x01\x00"
        await asyncio.wait_for(provider.waiting.wait(), 1)
        closing = asyncio.create_task(stream.aclose())
        await asyncio.wait_for(provider.close_started.wait(), 1)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done() and provider.closed == 0
        provider.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 2)
        run, fee = await rows(storage)
        assert provider.closed == 1 and run.status == "cancelled"
        assert fee.state == "unknown" and fee.charged_micros == 5000
    finally:
        provider.close_release.set()
        provider.release.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await stream.aclose()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_handoff_backpressure_does_not_read_an_unbounded_stream_ahead_of_consumer(
    backend: str, tmp_path: Path
) -> None:
    storage, owner, _, policy = await fixture(backend, tmp_path)
    third_read = asyncio.Event()

    class Endless(Stream):
        async def __anext__(self) -> bytes:
            self.reads += 1
            if self.reads == 3:
                third_read.set()
            return b"\x01\x00"

    provider = Endless()
    stream = execute(storage, owner, policy, lambda: provider)
    try:
        assert await anext(stream) == b"\x01\x00"
        await asyncio.wait_for(third_read.wait(), 1)
        await asyncio.sleep(0)
        assert provider.reads == 3  # Delivered, one queued, one blocked handoff.
        run, _ = await rows(storage)
        assert run.status == "running"
        await stream.aclose()
        run, fee = await rows(storage)
        assert run.status == "cancelled" and provider.closed == 1
        assert fee.charged_micros == 5000
    finally:
        await stream.aclose()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_queued_chunk_is_rechecked_after_consumer_pause_without_another_dispatch(
    backend: str, tmp_path: Path
) -> None:
    storage, owner, _, policy = await fixture(backend, tmp_path)
    provider, calls, revoked = Stream(), 0, False
    error = BudgetDenied("synthetic_source_changed_between_chunks")

    async def validate() -> None:
        if revoked:
            raise error

    def create() -> Stream:
        nonlocal calls
        calls += 1
        return provider

    stream = execute(storage, owner, policy, create, validate)
    try:
        assert await anext(stream) == b"\x01\x00"
        revoked = True
        with pytest.raises(BudgetDenied) as caught:
            await anext(stream)
        assert caught.value is error and calls == provider.closed == 1
        _, fee = await rows(storage)
        assert fee.state == "unknown" and fee.charged_micros == 5000
    finally:
        await stream.aclose()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_close_reports_terminal_transaction_failure_instead_of_silent_success(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.runs import operation
    from app.runs.store import transition_run

    storage, owner, _, policy = await fixture(backend, tmp_path)
    provider = Stream(wait=True)
    transition = transition_run
    error = RuntimeError("synthetic_terminal_sql_failure")

    async def fail_close(session: AsyncSession, identifier: UUID, status: str) -> None:
        if status == "cancelled":
            raise error
        await transition(session, identifier, status)

    monkeypatch.setattr(operation, "transition_run", fail_close)
    stream = execute(storage, owner, policy, lambda: provider)
    try:
        assert await anext(stream) == b"\x01\x00"
        await asyncio.wait_for(provider.waiting.wait(), 1)
        with pytest.raises(RuntimeError) as caught:
            await stream.aclose()
        assert caught.value is error and provider.closed == 1
        run, fee = await rows(storage)
        assert run.status == "running" and fee.state == "unknown" and fee.charged_micros == 5000
    finally:
        provider.release.set()
        await stream.aclose()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_producer_inherits_parent_context_and_stops_when_parent_is_cancelled(
    backend: str, tmp_path: Path
) -> None:
    storage, owner, root, policy = await fixture(backend, tmp_path)
    provider = Stream(wait=True)
    config = RunBudgetConfig.model_validate(dict(policy.budget))
    try:
        with budget_scope(
            RunModelBudget(storage.database, run_id=root, user_id=owner, config=config)
        ):
            stream = execute(storage, owner, policy, lambda: provider)
            assert await anext(stream) == b"\x01\x00"
            await asyncio.wait_for(provider.waiting.wait(), 1)
            run, fee = await rows(storage)
            assert run.parent_run_id == root and run.status == "running"
            async with storage.database.sessions.begin() as session:
                await session.execute(
                    update(TaskRunRecord).where(TaskRunRecord.id == root).values(status="cancelled")
                )
            with pytest.raises(BudgetDenied, match="budget_run_inactive"):
                await asyncio.wait_for(anext(stream), 2)
        run, fee = await rows(storage)
        assert run.status == "failed" and provider.closed == 1
        assert fee.state == "unknown" and fee.charged_micros == 5000
    finally:
        provider.release.set()
        await storage.close()
