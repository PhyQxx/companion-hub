"""BK-01 Admin 备份状态 API：目录只读检视、滚动配置与健康提示。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.admin_backups import create_admin_backups_router

AUTH = {"Authorization": "Bearer test-admin-token"}
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
STALE_HOURS = 26.0


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _app(backup_dir: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_admin_backups_router(
            backup_dir=backup_dir,
            keep_days=14,
            backup_at="03:30",
            timezone_name="Asia/Shanghai",
            admin_token="test-admin-token",
            clock=lambda: NOW,
        )
    )
    return app


@pytest.fixture
async def backup_dir(tmp_path: Path) -> AsyncIterator[Path]:
    yield tmp_path / "backups"


async def test_requires_admin_token(backup_dir: Path) -> None:
    async with _client(_app(backup_dir)) as client:
        response = await client.get("/api/v1/admin/backups")
    assert response.status_code == 401


async def test_missing_directory_reports_unavailable(backup_dir: Path) -> None:
    async with _client(_app(backup_dir)) as client:
        response = await client.get("/api/v1/admin/backups", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["healthy"] is False
    assert body["items"] == []
    assert body["keep_days"] == 14
    assert body["backup_at"] == "03:30"
    assert body["timezone"] == "Asia/Shanghai"


def _write_dump(directory: Path, name: str, age_hours: float, size: int = 1024) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(b"x" * size)
    stamp = NOW.timestamp() - age_hours * 3600
    os.utime(target, (stamp, stamp))


async def test_lists_dumps_newest_first_and_ignores_other_files(backup_dir: Path) -> None:
    _write_dump(backup_dir, "aria_20260920_033000.dump", age_hours=60)
    _write_dump(backup_dir, "aria_20260922_033000.dump", age_hours=8, size=4096)
    (backup_dir / "notes.txt").write_text("ignore me")
    (backup_dir / "aria_partial.tmp").write_text("not a dump")
    async with _client(_app(backup_dir)) as client:
        response = await client.get("/api/v1/admin/backups", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["healthy"] is True
    assert [item["file"] for item in body["items"]] == [
        "aria_20260922_033000.dump",
        "aria_20260920_033000.dump",
    ]
    assert body["items"][0]["size_bytes"] == 4096
    assert body["last_backup_at"] is not None


async def test_stale_backup_flagged_unhealthy(backup_dir: Path) -> None:
    _write_dump(backup_dir, "aria_20260920_033000.dump", age_hours=40)
    async with _client(_app(backup_dir)) as client:
        response = await client.get("/api/v1/admin/backups", headers=AUTH)
    body = response.json()
    assert body["available"] is True
    assert body["healthy"] is False


async def test_empty_directory_is_available_but_unhealthy(backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True)
    async with _client(_app(backup_dir)) as client:
        response = await client.get("/api/v1/admin/backups", headers=AUTH)
    body = response.json()
    assert body["available"] is True
    assert body["healthy"] is False
    assert body["last_backup_at"] is None


async def test_clock_injection_boundary(backup_dir: Path) -> None:
    """刚好在宽限期内/外的边界由注入时钟控制，不依赖真实时间。"""
    _write_dump(backup_dir, "aria_edge.dump", age_hours=STALE_HOURS - 0.5)
    async with _client(_app(backup_dir)) as client:
        fresh = await client.get("/api/v1/admin/backups", headers=AUTH)
    assert fresh.json()["healthy"] is True

    _write_dump(backup_dir, "aria_edge.dump", age_hours=STALE_HOURS + 0.5)
    async with _client(_app(backup_dir)) as client:
        stale = await client.get("/api/v1/admin/backups", headers=AUTH)
    assert stale.json()["healthy"] is False
