"""A derived transaction cannot retain admission after its lease expires."""

from __future__ import annotations

import os
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select, update
from test_claim_transaction import claim
from test_observation_owner_binding import accounts

from app.db import AppUserRecord, JobRecord
from app.db import claims as claim_storage
from app.harness.claim import ClaimInvalidated, claim_scope
from scripts.benchmark_storage import open_storage


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("phase", ["explicit_write", "pending_flush", "during_flush"])
async def test_expired_claim_rolls_back_derived_transaction(
    backend: str, phase: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "commit.db", url)
    database = storage.database
    now = [datetime.now(UTC)]

    class Clock:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return now[0]

    installed = False

    def after_execute(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("UPDATE ") and "app_user" in statement:
            now[0] += timedelta(seconds=2)

    try:
        owner, _ = await accounts(database)
        _, identity = await claim(database, owner)
        async with database.sessions.begin() as session:
            await session.execute(
                update(JobRecord)
                .where(JobRecord.id == identity.job_id)
                .values(lease_expires_at=now[0] + timedelta(seconds=1))
            )
        monkeypatch.setattr(claim_storage, "datetime", Clock)
        with pytest.raises(ClaimInvalidated):
            async with database.sessions.begin() as session:
                with claim_scope(identity):
                    await claim_storage.assert_current_claim(session)
                # The claim scope can finish before the Unit of Work exits.
                # A commit fence must belong to the transaction itself.
                if phase == "explicit_write":
                    await session.execute(
                        update(AppUserRecord)
                        .where(AppUserRecord.id == owner)
                        .values(display_name="Expired result")
                    )
                    now[0] += timedelta(seconds=2)
                else:
                    record = await session.get_one(AppUserRecord, owner)
                    record.display_name = "Expired result"
                    if phase == "pending_flush":
                        now[0] += timedelta(seconds=2)
                    else:
                        event.listen(
                            database.engine.sync_engine, "after_cursor_execute", after_execute
                        )
                        installed = True
        async with database.sessions.begin() as session:
            assert (
                await session.scalar(
                    select(AppUserRecord.display_name).where(AppUserRecord.id == owner)
                )
                == "First"
            )
    finally:
        if installed:
            event.remove(database.engine.sync_engine, "after_cursor_execute", after_execute)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("outcome", ["commit", "rollback", "expired"])
async def test_claim_commit_fence_is_cleared_when_session_is_reused(
    backend: str, outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "reuse.db", url)
    database = storage.database
    now = [datetime.now(UTC)]

    class Clock:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return now[0]

    try:
        owner, _ = await accounts(database)
        _, identity = await claim(database, owner)
        async with database.sessions.begin() as session:
            await session.execute(
                update(JobRecord)
                .where(JobRecord.id == identity.job_id)
                .values(lease_expires_at=now[0] + timedelta(seconds=1))
            )
        monkeypatch.setattr(claim_storage, "datetime", Clock)
        async with database.sessions() as session:
            expectation = pytest.raises(ClaimInvalidated) if outcome == "expired" else nullcontext()
            with expectation:
                async with session.begin():
                    with claim_scope(identity):
                        await claim_storage.assert_current_claim(session)
                    if outcome == "rollback":
                        await session.rollback()
                    elif outcome == "expired":
                        now[0] += timedelta(seconds=2)
            now[0] += timedelta(seconds=2)
            async with session.begin():
                await session.execute(
                    update(AppUserRecord)
                    .where(AppUserRecord.id == owner)
                    .values(display_name="Unrelated transaction")
                )
            assert (
                await session.scalar(
                    select(AppUserRecord.display_name).where(AppUserRecord.id == owner)
                )
                == "Unrelated transaction"
            )
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("lock_scope", ["outer", "savepoint"])
async def test_savepoint_rollback_cannot_retain_a_released_claim_lock(
    backend: str, lock_scope: str, tmp_path: Path
) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "savepoint.db", url)
    database = storage.database
    try:
        owner, _ = await accounts(database)
        _, identity = await claim(database, owner)
        expectation = (
            pytest.raises(ClaimInvalidated) if lock_scope == "savepoint" else nullcontext()
        )
        with expectation:
            async with database.sessions.begin() as session:
                if lock_scope == "outer":
                    with claim_scope(identity):
                        await claim_storage.assert_current_claim(session)
                savepoint = await session.begin_nested()
                if lock_scope == "savepoint":
                    with claim_scope(identity):
                        await claim_storage.assert_current_claim(session)
                await savepoint.rollback()
                await session.execute(
                    update(AppUserRecord)
                    .where(AppUserRecord.id == owner)
                    .values(display_name="Outer transaction")
                )
        async with database.sessions.begin() as session:
            assert await session.scalar(
                select(AppUserRecord.display_name).where(AppUserRecord.id == owner)
            ) == ("First" if lock_scope == "savepoint" else "Outer transaction")
    finally:
        await storage.close()
