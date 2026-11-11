import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from app.ids import uuid7


def test_nonempty_upgrade_preserves_memory_and_refuses_destructive_rollback(tmp_path: Path) -> None:
    path = tmp_path / "migration.db"
    environment = {**os.environ, "ARIA_DATABASE_URL": f"sqlite+aiosqlite:///{path}"}

    def migrate(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert migrate("upgrade", "0071_activity_summary").returncode == 0
    with sqlite3.connect(path) as connection:
        owner = uuid7().hex
        connection.execute("INSERT INTO app_user(id,display_name) VALUES (?, 'Fixture')", (owner,))
        connection.execute(
            "INSERT INTO memory(user_id,type,content,privacy_level,pin,access_count) "
            "VALUES (?,'semantic','synthetic migration fact','L1',1,999)",
            (owner,),
        )
        before = connection.execute(
            "SELECT id,user_id,content,status,updated_at FROM memory"
        ).fetchall()
    assert migrate("upgrade", "head").returncode == 0
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT id,user_id,content,status,updated_at FROM memory").fetchall()
            == before
        )
        assert connection.execute("SELECT tier,tier_locked,use_count FROM memory").fetchone() == (
            "medium",
            1,
            0,
        )
    failed = migrate("downgrade", "0071_activity_summary")
    assert failed.returncode != 0 and "Memory history must be retained" in failed.stderr
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT id,user_id,content,status,updated_at FROM memory").fetchall()
            == before
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0072_memory_lifecycle",
        )
