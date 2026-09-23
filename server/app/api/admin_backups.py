"""BK-01 备份状态可视化：只读列出备份目录与滚动保留配置。

Hub 对备份目录只读（compose 以 :ro 挂载）；转储/清理由 backup profile
的独立容器执行，本端点只做检视与健康提示，不提供删除或触发备份入口
（避免与 backup.sh 的补跑/重试语义冲突）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends

from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard

DUMP_PATTERN = "aria_*.dump"
# 每日一备 + 补跑重试的宽限：超过该时长未出现新备份即视为不健康
STALE_AFTER_HOURS = 26
MAX_LISTED = 30


class BackupFileInfo(StrictModel):
    file: str
    size_bytes: int
    modified_at: datetime


class AdminBackupsResponse(StrictModel):
    available: bool
    dir: str
    keep_days: int
    backup_at: str
    timezone: str
    stale_after_hours: int
    items: list[BackupFileInfo]
    last_backup_at: datetime | None = None
    healthy: bool


def create_admin_backups_router(
    *,
    backup_dir: Path,
    keep_days: int,
    backup_at: str,
    timezone_name: str,
    admin_token: str | None,
    clock: Callable[[], datetime] | None = None,
) -> APIRouter:
    now = clock or (lambda: datetime.now(UTC))

    router = APIRouter(
        prefix="/api/v1/admin/backups",
        tags=["admin-backups"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("", response_model=AdminBackupsResponse)
    async def list_backups() -> AdminBackupsResponse:
        items: list[BackupFileInfo] = []
        if backup_dir.is_dir():
            for path in backup_dir.glob(DUMP_PATTERN):
                try:
                    stat = path.stat()
                except OSError:  # 并发清理窗口内消失的文件直接跳过
                    continue
                items.append(
                    BackupFileInfo(
                        file=path.name,
                        size_bytes=stat.st_size,
                        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    )
                )
        items.sort(key=lambda item: item.modified_at, reverse=True)
        items = items[:MAX_LISTED]
        last = items[0].modified_at if items else None
        # 目录缺失（备份 profile 未启用或挂载错误）同样视为不健康，便于 Admin 一眼发现
        healthy = backup_dir.is_dir() and last is not None and (
            now() - last
        ).total_seconds() <= STALE_AFTER_HOURS * 3600
        return AdminBackupsResponse(
            available=backup_dir.is_dir(),
            dir=str(backup_dir),
            keep_days=keep_days,
            backup_at=backup_at,
            timezone=timezone_name,
            stale_after_hours=STALE_AFTER_HOURS,
            items=items,
            last_backup_at=last,
            healthy=healthy,
        )

    return router
