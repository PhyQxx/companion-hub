import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from app.ids import uuid7


def test_cost_migration_keeps_nonempty_ledger_during_rollback(tmp_path: Path) -> None:
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

    assert migrate("upgrade", "head").returncode == 0
    assert migrate("downgrade", "0061_model_budget").returncode == 0
    assert migrate("upgrade", "head").returncode == 0
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO model_cost (call_id,user_id,endpoint,state,charged_micros,created_at) "
            "VALUES (?,?,?,'unknown',800,'2026-10-02 00:00:00')",
            (uuid7().hex, uuid7().hex, "synthetic"),
        )
    failed = migrate("downgrade", "0061_model_budget")
    assert failed.returncode != 0
    assert "ledger must be retained" in failed.stderr
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT charged_micros FROM model_cost").fetchone() == (800,)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0062_model_cost",
        )
