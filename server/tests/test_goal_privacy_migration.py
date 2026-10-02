import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from app.ids import uuid7


def test_nullable_goal_privacy_upgrade_preserves_legacy_and_refuses_label_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "privacy-migration.db"
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

    assert migrate("upgrade", "0062_model_cost").returncode == 0
    owner, goal = uuid7().hex, uuid7().hex
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO app_user (id,display_name) VALUES (?, 'Fixture')", (owner,))
        connection.execute(
            "INSERT INTO cognitive_goal (id,user_id,kind,title,status,source_kind,"
            "source_id,created_at,updated_at) "
            "VALUES (?,?,'user','synthetic legacy','active','manual','fixture',"
            "'2026-10-02 00:00:00','2026-10-02 00:00:00')",
            (goal, owner),
        )
    assert migrate("upgrade", "head").returncode == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT title,privacy_level FROM cognitive_goal").fetchone() == (
            "synthetic legacy",
            None,
        )
    assert migrate("downgrade", "0062_model_cost").returncode == 0
    assert migrate("upgrade", "head").returncode == 0
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE cognitive_goal SET privacy_level='L2' WHERE id=?", (goal,))
    failed = migrate("downgrade", "0062_model_cost")
    assert failed.returncode != 0 and "privacy evidence must be retained" in failed.stderr
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT title,privacy_level FROM cognitive_goal").fetchone() == (
            "synthetic legacy",
            "L2",
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0063_goal_privacy",
        )
