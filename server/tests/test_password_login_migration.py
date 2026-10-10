"""0070 用户名登录迁移：列回建、唯一约束与占用保留拒绝回退。"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, inspect, text
from test_observation_binding_migration import revision
from test_run_cancel_fence import prepared


def test_password_login_downgrade_refuses_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    module = revision("0070_password_login.py")
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: True)
    with pytest.raises(RuntimeError, match="cannot be checked offline"):
        module.downgrade()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("case", ["clean", "named"])
async def test_password_login_migration(
    backend: str, case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await prepared(backend, tmp_path)
    module = revision("0070_password_login.py")
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: False)

    def migrate(connection: Connection) -> None:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(module, "op", operations)
        schema = storage.schema
        # 回到 0069 形态：去掉 0070 增加的列/唯一约束
        module.downgrade()
        inspector = inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("app_user", schema=schema)}
        assert "username" not in columns

        owner = uuid4()
        created_at = "CURRENT_TIMESTAMP" if backend == "sqlite" else "now()"
        connection.execute(
            text(
                "INSERT INTO app_user"
                " (id, display_name, locale, timezone, status, role, created_at)"
                f" VALUES (:id, '业主', 'zh-CN', 'Asia/Shanghai', 'active', 'owner', {created_at})"
            ),
            {"id": owner.hex if backend == "sqlite" else owner},
        )
        other = uuid4()
        connection.execute(
            text(
                "INSERT INTO app_user"
                " (id, display_name, locale, timezone, status, role, created_at)"
                f" VALUES (:id, '成员', 'zh-CN', 'Asia/Shanghai', 'active', 'member', {created_at})"
            ),
            {"id": other.hex if backend == "sqlite" else other},
        )

        module.upgrade()
        inspector = inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("app_user", schema=schema)}
        assert "username" in columns

        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(text("UPDATE app_user SET username = 'dup'"))

        if case == "named":
            connection.execute(
                text("UPDATE app_user SET username = 'owner-account' WHERE role = 'owner'")
            )
            with pytest.raises(RuntimeError, match="bindings must be retained"):
                module.downgrade()
            assert "username" in {
                column["name"]
                for column in inspect(connection).get_columns("app_user", schema=schema)
            }
        else:
            module.downgrade()
            assert "username" not in {
                column["name"]
                for column in inspect(connection).get_columns("app_user", schema=schema)
            }

    try:
        async with storage.database.engine.begin() as connection:
            await connection.run_sync(migrate)
    finally:
        await storage.close()


def test_full_sqlite_chain_reaches_password_login_head(tmp_path: Path) -> None:
    path = tmp_path / "password-login-migration.db"
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
            "0070_password_login",
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(app_user)")}
        assert "username" in columns
