"""Single-use authorization migration is isolated from any running database."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Table, inspect, select
from test_observation_binding_migration import revision
from test_operation_authority_fence import prepared

from app.auth import AuthService
from app.db import CalendarOAuthStateRecord
from app.ids import uuid7


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("retained", [False, True])
async def test_oauth_state_upgrade_and_retained_authority_blocks_downgrade(
    backend: str,
    retained: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        account = await AuthService(storage.database).setup(
            display_name="Migration fixture", password="synthetic migration password"
        )
        module = revision("0068_calendar_oauth_state.py")
        monkeypatch.setattr(module.context, "is_offline_mode", lambda: False)

        def migrate(connection: Connection) -> None:
            ops = Operations(MigrationContext.configure(connection))
            monkeypatch.setattr(module, "op", ops)
            ops.drop_table("calendar_oauth_state")
            module.upgrade()
            inspector = inspect(connection)
            constraints = {
                tuple(row["column_names"])
                for row in inspector.get_unique_constraints(
                    "calendar_oauth_state", schema=storage.schema
                )
            }
            assert constraints == {("user_id",), ("state_hash",)}
            fks = {
                row["referred_table"]
                for row in inspector.get_foreign_keys("calendar_oauth_state", schema=storage.schema)
            }
            assert fks == {"app_user", "auth_session"}
            if retained:
                now = datetime.now(UTC)
                connection.execute(
                    cast(Table, CalendarOAuthStateRecord.__table__)
                    .insert()
                    .values(
                        id=uuid7(),
                        user_id=account.principal.user_id,
                        session_id=account.principal.session_id,
                        state_hash="a" * 64,
                        config_version=1,
                        config_hash="b" * 64,
                        created_at=now,
                        expires_at=now + timedelta(minutes=10),
                    )
                )
                with pytest.raises(RuntimeError, match="must be retained"):
                    module.downgrade()
                assert connection.scalar(select(CalendarOAuthStateRecord.id)) is not None
            else:
                module.downgrade()
                assert "calendar_oauth_state" not in inspect(connection).get_table_names(
                    schema=storage.schema
                )
                module.upgrade()

        async with storage.database.engine.begin() as connection:
            await connection.run_sync(migrate)
    finally:
        await storage.close()


def test_oauth_state_downgrade_refuses_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    module = revision("0068_calendar_oauth_state.py")
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: True)
    with pytest.raises(RuntimeError, match="cannot be checked offline"):
        module.downgrade()
