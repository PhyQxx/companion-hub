"""Private task upgrade preserves rows; rollback cannot erase their privacy."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Table, inspect, select
from sqlalchemy.exc import IntegrityError
from test_commute_query_boundary import NOW
from test_observation_binding_migration import revision
from test_run_cancel_fence import prepared

from app.db import AppUserRecord, TaskItemRecord


def test_private_task_downgrade_refuses_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    module = revision("0066_task_private.py")
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: True)
    with pytest.raises(RuntimeError, match="cannot be checked offline"):
        module.downgrade()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("retained", [False, True])
async def test_private_task_migration_preserves_data_and_constraints(
    backend: str, retained: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await prepared(backend, tmp_path)
    module = revision("0066_task_private.py")
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: False)

    def migrate(connection: Connection) -> None:
        monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(connection)))
        table, schema = cast(Table, TaskItemRecord.__table__), storage.schema
        before = inspect(connection)
        indexes = before.get_indexes("task_item", schema=schema)
        foreign_keys = before.get_foreign_keys("task_item", schema=schema)
        primary_key = before.get_pk_constraint("task_item", schema=schema)
        other_checks = {
            value["name"]: value["sqltext"]
            for value in before.get_check_constraints("task_item", schema=schema)
            if value["name"] != "ck_task_item_privacy_level"
        }
        module.downgrade()
        owner, legacy = uuid4(), uuid4()
        connection.execute(
            cast(Table, AppUserRecord.__table__)
            .insert()
            .values(id=owner, display_name="Fixture", status="active")
        )

        def insert_task(level: str, identifier: object) -> None:
            connection.execute(
                table.insert().values(
                    id=identifier,
                    user_id=owner,
                    kind="task",
                    title="Preserved fixture",
                    status="active",
                    trigger_type="time",
                    trigger_config={"type": "time", "at": NOW.isoformat()},
                    source="fixture",
                    privacy_level=level,
                    fire_count=0,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

        insert_task("L1", legacy)
        module.upgrade()
        assert connection.scalar(select(table.c.privacy_level).where(table.c.id == legacy)) == "L1"
        after = inspect(connection)
        assert after.get_indexes("task_item", schema=schema) == indexes
        assert after.get_foreign_keys("task_item", schema=schema) == foreign_keys
        assert after.get_pk_constraint("task_item", schema=schema) == primary_key
        assert {
            value["name"]: value["sqltext"]
            for value in after.get_check_constraints("task_item", schema=schema)
            if value["name"] != "ck_task_item_privacy_level"
        } == other_checks
        assert "'L2'" in next(
            value["sqltext"]
            for value in after.get_check_constraints("task_item", schema=schema)
            if value["name"] == "ck_task_item_privacy_level"
        )
        with pytest.raises(IntegrityError), connection.begin_nested():
            insert_task("L3", uuid4())
        if retained:
            private = uuid4()
            insert_task("L2", private)
            with pytest.raises(RuntimeError, match="evidence must be retained"):
                module.downgrade()
            assert (
                connection.scalar(select(table.c.privacy_level).where(table.c.id == private))
                == "L2"
            )
        else:
            module.downgrade()
            assert (
                connection.scalar(select(table.c.title).where(table.c.id == legacy))
                == "Preserved fixture"
            )
            with pytest.raises(IntegrityError), connection.begin_nested():
                insert_task("L2", uuid4())
            module.upgrade()

    try:
        async with storage.database.engine.begin() as connection:
            await connection.run_sync(migrate)
    finally:
        await storage.close()


def test_full_sqlite_chain_reaches_private_task_head(tmp_path: Path) -> None:
    path = tmp_path / "private-task-chain.db"
    environment = {**os.environ, "ARIA_DATABASE_URL": f"sqlite+aiosqlite:///{path}"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0066_task_private"],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0066_task_private",
        )
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_item'"
        ).fetchone()[0]
        assert "privacy_level IN ('L0','L1','L2')" in definition
