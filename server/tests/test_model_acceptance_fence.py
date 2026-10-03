"""Independent model runs fence owners and sources before recording acceptance."""

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select, update
from test_delivery_sqlite_transactions import explicit_transactions
from test_job_lifecycle_fence import during_commit
from test_model_runs import setup

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, ConversationRecord, TaskRunRecord
from app.harness.budget import BudgetDenied
from app.harness.time import utc
from app.ids import uuid7
from app.llm.contracts import CompletionRequest, CompletionResult
from app.runs import completion
from app.runs.store import append_run_event, transition_run
from scripts.benchmark_storage import FixtureStorage, open_storage


async def prepared(backend: str, tmp_path: Path) -> FixtureStorage:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    return await open_storage(tmp_path / "model.db", url)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("sourced", [False, True])
async def test_model_waiting_owner_deactivation_cannot_record_acceptance(
    backend: str, sourced: bool, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    calls = 0

    async def invoke(request: CompletionRequest) -> CompletionResult:
        nonlocal calls
        calls += 1
        return CompletionResult(
            text="fixture",
            endpoint="local",
            model="fixture",
            provider="fixture",
            route=request.route,
            latency_ms=0,
        )

    try:
        owner, snapshot, request = await setup(storage.database, tmp_path)
        snapshot = replace(
            snapshot,
            config=snapshot.config.model_copy(
                update={"run_budget": RunBudgetConfig(enabled=False)}
            ),
        )
        source = uuid7() if sourced else None
        if source:
            async with storage.database.sessions.begin() as session:
                session.add(ConversationRecord(id=source, user_id=owner, title="Fixture"))

        async def rejected() -> None:
            with pytest.raises(BudgetDenied, match="budget_owner_invalid"):
                await completion.complete_with_run(
                    storage.database,
                    snapshot,
                    request,
                    invoke,
                    user_id=owner,
                    kind="fixture.model",
                    source_id=uuid7(),
                    conversation_id=source,
                )

        await during_commit(
            storage.database,
            lambda session: session.execute(
                update(AppUserRecord).where(AppUserRecord.id == owner).values(status="inactive")
            ),
            rejected,
        )
        assert calls == 0
        async with storage.database.sessions() as session:
            assert list(await session.scalars(select(TaskRunRecord))) == []
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("sourced", [False, True])
@pytest.mark.parametrize("stage", ["accept", "result"])
async def test_model_acceptance_keeps_owner_locked_through_commit(
    backend: str, sourced: bool, stage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await prepared(backend, tmp_path)
    attempted = asyncio.Event()
    task: asyncio.Task[None] | None = None
    overlaps: list[bool] = []

    def before_execute(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("UPDATE ") and any(
            values.get("status") == "inactive" for values in context.compiled_parameters
        ):
            attempted.set()

    async def invoke(request: CompletionRequest) -> CompletionResult:
        return CompletionResult(
            text="fixture",
            endpoint="local",
            model="fixture",
            provider="fixture",
            route=request.route,
            latency_ms=0,
        )

    try:
        owner, snapshot, request = await setup(storage.database, tmp_path)
        source = uuid7() if sourced else None
        if source:
            async with storage.database.sessions.begin() as session:
                session.add(ConversationRecord(id=source, user_id=owner, title="Fixture"))

        async def deactivate() -> None:
            async with storage.database.sessions.begin() as session:
                await session.execute(
                    update(AppUserRecord).where(AppUserRecord.id == owner).values(status="inactive")
                )

        async def before_commit() -> None:
            nonlocal task
            task = asyncio.create_task(deactivate())
            await asyncio.wait_for(attempted.wait(), 3)
            await asyncio.sleep(0.05)
            overlaps.append(task.done())

        async def append(*args: Any, **kwargs: Any) -> None:
            await append_run_event(*args, **kwargs)
            if stage == "accept":
                await before_commit()

        async def transition(*args: Any, **kwargs: Any) -> None:
            await transition_run(*args, **kwargs)
            if stage == "result" and args[2] == "succeeded":
                await before_commit()

        monkeypatch.setattr(completion, "append_run_event", append)
        monkeypatch.setattr(completion, "transition_run", transition)
        event.listen(storage.database.engine.sync_engine, "before_cursor_execute", before_execute)
        try:
            try:
                await completion.complete_with_run(
                    storage.database,
                    snapshot,
                    request,
                    invoke,
                    user_id=owner,
                    kind="fixture.model",
                    source_id=uuid7(),
                    conversation_id=source,
                )
            except BudgetDenied as error:
                assert error.reason_code == "budget_owner_invalid"
            assert task is not None
            await asyncio.wait_for(task, 3)
            assert overlaps == [False]
        finally:
            event.remove(
                storage.database.engine.sync_engine, "before_cursor_execute", before_execute
            )
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_model_deadline_expiring_in_acceptance_event_rolls_back(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await prepared(backend, tmp_path)
    now = [datetime(2030, 1, 1, tzinfo=UTC)]

    class Clock:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return now[0]

    async def invoke(request: CompletionRequest) -> CompletionResult:
        raise AssertionError("Expired model admission must not invoke the provider")

    async def append(*args: Any, **kwargs: Any) -> None:
        await append_run_event(*args, **kwargs)
        now[0] = utc(args[1].deadline) + timedelta(seconds=1)

    monkeypatch.setattr(completion, "datetime", Clock)
    monkeypatch.setattr(completion, "append_run_event", append)
    try:
        owner, snapshot, request = await setup(storage.database, tmp_path)
        with pytest.raises(BudgetDenied, match="run_deadline_exceeded"):
            await completion.complete_with_run(
                storage.database,
                snapshot,
                request,
                invoke,
                user_id=owner,
                kind="fixture.model",
                source_id=uuid7(),
            )
        async with storage.database.sessions() as session:
            assert list(await session.scalars(select(TaskRunRecord))) == []
    finally:
        await storage.close()


async def test_explicit_sqlite_parallel_models_write_source_before_reading(tmp_path: Path) -> None:
    storage = await prepared("sqlite", tmp_path)

    async def invoke(request: CompletionRequest) -> CompletionResult:
        return CompletionResult(
            text="fixture",
            endpoint="local",
            model="fixture",
            provider="fixture",
            route=request.route,
            latency_ms=0,
        )

    try:
        owner, snapshot, request = await setup(storage.database, tmp_path)
        source = uuid7()
        async with storage.database.sessions.begin() as session:
            session.add(ConversationRecord(id=source, user_id=owner, title="Fixture"))
        snapshot = replace(
            snapshot,
            config=snapshot.config.model_copy(
                update={"run_budget": RunBudgetConfig(enabled=False)}
            ),
        )
        async with explicit_transactions(storage.database):
            results = await asyncio.gather(
                *[
                    completion.complete_with_run(
                        storage.database,
                        snapshot,
                        request,
                        invoke,
                        user_id=owner,
                        kind="fixture.model",
                        source_id=uuid7(),
                        conversation_id=source,
                    )
                    for _ in range(8)
                ],
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            assert all(isinstance(result, CompletionResult) for result in results), results
        async with storage.database.sessions() as session:
            rows = list(await session.scalars(select(TaskRunRecord)))
            assert len(rows) == 8 and all(row.status == "succeeded" for row in rows)
    finally:
        await storage.close()
