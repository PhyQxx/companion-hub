"""0069 多用户身份迁移：列/约束回建、存量活跃用户升业主、绑定保留拒绝回退。"""

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


def test_multiuser_downgrade_refuses_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    module = revision("0069_multiuser_identity.py")
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: True)
    with pytest.raises(RuntimeError, match="cannot be checked offline"):
        module.downgrade()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("case", ["clean", "bound"])
async def test_multiuser_identity_migration(
    backend: str, case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await prepared(backend, tmp_path)
    module = revision("0069_multiuser_identity.py")
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: False)

    def migrate(connection: Connection) -> None:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(module, "op", operations)
        schema = storage.schema
        # 回到 0068 形态：去掉 0069 增加的列/约束/索引
        module.downgrade()
        inspector = inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("app_user", schema=schema)}
        assert {"sso_sub", "role"} & columns == set()

        # 存量单用户：本地密码业主 + 一名停用账户
        legacy = uuid4()
        disabled = uuid4()
        created_at = "CURRENT_TIMESTAMP" if backend == "sqlite" else "now()"
        connection.execute(
            text(
                "INSERT INTO app_user (id, display_name, locale, timezone, status, created_at)"
                f" VALUES (:id, '存量业主', 'zh-CN', 'Asia/Shanghai', 'active', {created_at})"
            ),
            {"id": legacy.hex if backend == "sqlite" else legacy},
        )
        connection.execute(
            text(
                "INSERT INTO app_user (id, display_name, locale, timezone, status, created_at)"
                f" VALUES (:id, '停用账户', 'zh-CN', 'Asia/Shanghai', 'disabled', {created_at})"
            ),
            {"id": disabled.hex if backend == "sqlite" else disabled},
        )

        module.upgrade()
        inspector = inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("app_user", schema=schema)}
        assert {"sso_sub", "role"} <= columns
        roles = {
            str(row[0]).replace("-", ""): row[1]
            for row in connection.execute(text("SELECT id, role FROM app_user"))
        }
        assert roles[legacy.hex] == "owner", "存量活跃用户应升为业主"
        assert roles[disabled.hex] == "member"
        # role 约束生效
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text("UPDATE app_user SET role = 'root' WHERE id = :id"),
                {"id": legacy.hex if backend == "sqlite" else legacy},
            )
        # sso_sub 唯一约束生效（两条 NULL 不冲突，绑定值必须唯一）
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text("UPDATE app_user SET sso_sub = 'dup-sub'"),
            )

        if case == "bound":
            connection.execute(
                text("UPDATE app_user SET sso_sub = 'owner-sub' WHERE role = 'owner'")
            )
            with pytest.raises(RuntimeError, match="bindings must be retained"):
                module.downgrade()
            assert "sso_sub" in {
                column["name"]
                for column in inspect(connection).get_columns("app_user", schema=schema)
            }
        else:
            module.downgrade()
            assert "sso_sub" not in {
                column["name"]
                for column in inspect(connection).get_columns("app_user", schema=schema)
            }

    try:
        async with storage.database.engine.begin() as connection:
            await connection.run_sync(migrate)
    finally:
        await storage.close()


def test_full_sqlite_chain_reaches_multiuser_head(tmp_path: Path) -> None:
    path = tmp_path / "multiuser-migration.db"
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
            "0069_multiuser_identity",
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(app_user)")}
        assert {"sso_sub", "role"} <= columns
