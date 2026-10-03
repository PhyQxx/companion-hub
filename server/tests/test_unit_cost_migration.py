"""Unit ledger upgrades preserve token records; populated unit evidence blocks rollback."""

import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Table, inspect, select
from sqlalchemy.exc import IntegrityError
from test_observation_binding_migration import revision
from test_run_cancel_fence import prepared

from app.db import ModelCostRecord


def test_unit_cost_downgrade_refuses_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    module = revision("0067_unit_costs.py")
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: True)
    with pytest.raises(RuntimeError, match="cannot be checked offline"):
        module.downgrade()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("retained", ["none", "reserved", "estimated", "unknown"])
async def test_unit_cost_upgrade_preserves_legacy_and_retains_all_quoted_evidence(
    backend: str,
    retained: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await prepared(backend, tmp_path)
    module = revision("0067_unit_costs.py")
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: False)

    def migrate(connection: Connection) -> None:
        monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(connection)))
        table, schema = cast(Table, ModelCostRecord.__table__), storage.schema
        before = inspect(connection)
        indexes = before.get_indexes("model_cost", schema=schema)
        primary = before.get_pk_constraint("model_cost", schema=schema)
        checks = {
            value["name"]: value["sqltext"]
            for value in before.get_check_constraints("model_cost", schema=schema)
            if value["name"] not in module._CHECKS
        }
        assert before.get_foreign_keys("model_cost", schema=schema) == []
        module.downgrade()
        legacy, owner = uuid4(), uuid4()
        now = datetime.now(UTC)
        legacy_table = Table("model_cost", table.metadata.__class__(), autoload_with=connection)
        connection.execute(
            legacy_table.insert().values(
                call_id=legacy.hex if backend == "sqlite" else legacy,
                user_id=owner.hex if backend == "sqlite" else owner,
                endpoint="token-fixture",
                currency="CNY",
                input_rate=Decimal("0.001"),
                output_rate=Decimal("0.002"),
                reserved_micros=4,
                charged_micros=2,
                state="estimated",
                created_at=now,
                provider_request_id="legacy-receipt",
            )
        )
        module.upgrade()
        after = inspect(connection)
        assert after.get_indexes("model_cost", schema=schema) == indexes
        assert after.get_pk_constraint("model_cost", schema=schema) == primary
        assert after.get_foreign_keys("model_cost", schema=schema) == []
        assert {
            value["name"]: value["sqltext"]
            for value in after.get_check_constraints("model_cost", schema=schema)
            if value["name"] not in module._CHECKS
        } == checks
        current = (
            connection.execute(select(table).where(table.c.call_id == legacy)).mappings().one()
        )
        assert (current["charged_micros"], current["provider_request_id"], current["unit"]) == (
            2,
            "legacy-receipt",
            None,
        )
        assert current["input_rate"] == Decimal("0.001")
        unit_id = uuid4()
        arguments = dict(
            call_id=unit_id,
            user_id=owner,
            endpoint="unit-fixture",
            currency="CNY",
            unit="second",
            unit_rate=Decimal("0.000000000001"),
            unit_maximum_quantity=Decimal("1.000000000001"),
            charged_micros=0,
            reserved_micros=1,
            state="unknown",
            created_at=now,
        )
        for changes in (
            {"unit": "token"},
            {"unit_rate": Decimal(-1)},
            {"unit_maximum_quantity": Decimal(0)},
            {"unit_quantity": Decimal(-1)},
            {"input_rate": Decimal(0)},
            {"currency": None},
            {"state": "estimated"},
            {"unit": None},
        ):
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(table.insert().values(**{**arguments, **changes}))
        if retained != "none":
            connection.execute(
                table.insert().values(
                    **{**arguments, "state": retained, "unit_quantity": Decimal(0)}
                )
            )
            with pytest.raises(RuntimeError, match="evidence must be retained"):
                module.downgrade()
            assert connection.scalar(
                select(table.c.unit_rate).where(table.c.call_id == unit_id)
            ) == (Decimal("0.000000000001"))
            assert connection.scalar(
                select(table.c.unit_maximum_quantity).where(table.c.call_id == unit_id)
            ) == Decimal("1.000000000001")
        else:
            module.downgrade()
            legacy_table = Table("model_cost", table.metadata.__class__(), autoload_with=connection)
            row = connection.execute(select(legacy_table)).mappings().one()
            assert (row["charged_micros"], row["provider_request_id"]) == (2, "legacy-receipt")
            assert "unit" not in row

    try:
        async with storage.database.engine.begin() as connection:
            await connection.run_sync(migrate)
    finally:
        await storage.close()


def test_full_sqlite_chain_reaches_unit_cost_head(tmp_path: Path) -> None:
    path = tmp_path / "unit-migration.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "ARIA_DATABASE_URL": f"sqlite+aiosqlite:///{path}"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0067_unit_costs",
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(model_cost)")}
        assert {"unit", "unit_rate", "unit_maximum_quantity", "unit_quantity"} <= columns
