"""Quota migration retains spent counts even when business records are absent."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, inspect, text
from test_observation_binding_migration import revision

from scripts.benchmark_storage import open_storage


def test_quota_downgrade_refuses_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    module = revision("0065_proactive_quota.py")
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: True)
    with pytest.raises(RuntimeError, match="cannot be checked offline"):
        module.downgrade()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("retained", [False, True])
async def test_quota_migration(
    backend: str, retained: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL") if backend == "postgresql" else None
    if backend == "postgresql" and url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "quota.db", url)
    module = revision("0065_proactive_quota.py")
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: False)

    def migrate(connection: Connection) -> None:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(module, "op", operations)
        operations.drop_table("proactive_quota_entry")
        module.upgrade()
        schema = storage.schema
        inspector = inspect(connection)
        assert [
            column["name"]
            for column in inspector.get_columns("proactive_quota_entry", schema=schema)
        ] == ["decision_id", "user_id", "accepted_at"]
        assert inspector.get_pk_constraint("proactive_quota_entry", schema=schema)[
            "constrained_columns"
        ] == ["decision_id"]
        assert not inspector.get_foreign_keys("proactive_quota_entry", schema=schema)
        assert inspector.get_indexes("proactive_quota_entry", schema=schema)[0]["column_names"] == [
            "user_id",
            "accepted_at",
        ]
        if retained:
            owner, decision = uuid4(), uuid4()
            connection.execute(
                text(
                    "INSERT INTO proactive_quota_entry (decision_id, user_id, accepted_at) "
                    "VALUES (:decision, :owner, CURRENT_TIMESTAMP)"
                ),
                {
                    "owner": owner.hex if backend == "sqlite" else owner,
                    "decision": decision.hex if backend == "sqlite" else decision,
                },
            )
            with pytest.raises(RuntimeError, match="evidence must be retained"):
                module.downgrade()
            assert connection.scalar(text("SELECT count(*) FROM proactive_quota_entry")) == 1
        else:
            module.downgrade()
            assert not inspect(connection).has_table("proactive_quota_entry", schema=schema)

    try:
        async with storage.database.engine.begin() as connection:
            await connection.run_sync(migrate)
    finally:
        await storage.close()
