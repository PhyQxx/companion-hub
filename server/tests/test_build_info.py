import json
import os
import subprocess
import sys
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.build_info import build_info
from app.main import create_app


def test_build_stamp_changes_with_source(tmp_path: Path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "deploy/write-build-info.py"
    for name in ("pyproject.toml", "uv.lock", "alembic.ini"):
        (tmp_path / name).write_text("test")
    (tmp_path / "server").mkdir()
    source = tmp_path / "server/test.py"
    source.write_text("first")
    env = {**os.environ, "BUILD_REVISION": ""}
    subprocess.run([sys.executable, str(script), str(tmp_path)], check=True, env=env)
    first = json.loads((tmp_path / "build-info.json").read_text())
    source.write_text("second")
    subprocess.run([sys.executable, str(script), str(tmp_path)], check=True, env=env)
    second = json.loads((tmp_path / "build-info.json").read_text())
    assert first["source_digest"] != second["source_digest"]
    monkeypatch.setenv("ARIA_BUILD_INFO_PATH", str(tmp_path / "build-info.json"))
    monkeypatch.setenv("ARIA_DEPLOYED_AT", "2026-11-11T12:00:00Z")
    assert build_info()["version"] == f"0.1.0+{second['source_digest']}"
    assert build_info()["deployed_at"] == "2026-11-11T12:00:00Z"
    assert build_info()["built_at"] == second["built_at"]


async def test_public_version_is_uncached_and_start_time_stable(monkeypatch):
    monkeypatch.delenv("ARIA_DATABASE_URL", raising=False)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/api/v1/meta/version")
        second = await client.get("/api/v1/meta/version")
        health = await client.get("/healthz")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert first.json() == second.json() == health.json()["deployment"]
