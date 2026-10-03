"""Binding migration and evidence-preserving downgrade on both fixture backends."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, inspect, text

from scripts.benchmark_storage import open_storage


def revision(filename: str = "0064_observation_owner.py") -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location("observation_binding_revision", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_binding_downgrade_refuses_offline_without_emitting_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = revision()
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: True)
    with pytest.raises(RuntimeError, match="cannot be checked offline"):
        module.downgrade()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("case", ["empty", "retained", "invalid_slot"])
async def test_binding_migration(
    backend: str, case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "migration.db", url)
    module = revision()
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: False)

    def migrate(connection: Connection) -> None:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(module, "op", operations)
        operations.drop_table("observation_owner_binding")
        module.upgrade()
        schema = storage.schema
        inspector = inspect(connection)
        columns = inspector.get_columns("observation_owner_binding", schema=schema)
        assert [column["name"] for column in columns] == ["slot", "user_id", "created_at"]
        assert inspector.get_pk_constraint("observation_owner_binding", schema=schema)[
            "constrained_columns"
        ] == ["slot"]
        assert not inspector.get_foreign_keys("observation_owner_binding", schema=schema)
        assert any(
            constraint["name"] == "ck_observation_owner_binding_slot"
            for constraint in inspector.get_check_constraints(
                "observation_owner_binding", schema=schema
            )
        )
        if case == "empty":
            module.downgrade()
            assert not inspect(connection).has_table("observation_owner_binding", schema=schema)
        elif case == "retained":
            # Deliberately absent app_user: the UUID must survive deletion.
            owner = uuid4()
            connection.execute(
                text("INSERT INTO observation_owner_binding (slot, user_id) VALUES (1, :owner)"),
                {"owner": owner.hex if backend == "sqlite" else owner},
            )
            with pytest.raises(RuntimeError, match="evidence must be retained"):
                module.downgrade()
            assert connection.scalar(text("SELECT count(*) FROM observation_owner_binding")) == 1
        else:
            from sqlalchemy.exc import IntegrityError

            # The savepoint exits before pytest catches the constraint error.
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO observation_owner_binding (slot, user_id) VALUES (2, :owner)"
                    ),
                    {"owner": uuid4().hex if backend == "sqlite" else uuid4()},
                )
            assert connection.scalar(text("SELECT count(*) FROM observation_owner_binding")) == 0
            module.downgrade()

    try:
        async with storage.database.engine.begin() as connection:
            await connection.run_sync(migrate)
    finally:
        await storage.close()
