"""HRM Admin：hermes 服务器代理连接状态、额度与最近任务（只读检视）。

不做配置写入（配置仍走统一 config 管理）；recent_runs 为进程内存记录，
重启后为空——完整任务历史走 Admin Jobs（deleg.hermes_task）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.integrations.hermes import HermesManager
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard


class HermesHealthItem(StrictModel):
    status: str
    version: str | None = None
    reason_code: str | None = None
    checked_at: str | None = None
    latency_ms: float | None = None


class HermesUsageItem(StrictModel):
    user_id: str
    daily_count: int
    active_count: int


class HermesRecentRunItem(StrictModel):
    run_id: str | None
    user_id: str | None
    status: str
    reason_code: str | None = None
    instruction_chars: int | None = None
    output_chars: int | None = None
    waited_for_approval: bool | None = None
    timeout_seconds: int | None = None


class HermesStatusResponse(StrictModel):
    enabled: bool
    health: HermesHealthItem
    limits: dict[str, int]
    usage: list[HermesUsageItem]
    recent_runs: list[HermesRecentRunItem]


def _health_item(manager: HermesManager) -> HermesHealthItem:
    health = manager.health
    return HermesHealthItem(
        status=health.status,
        version=health.version,
        reason_code=health.reason_code,
        checked_at=health.checked_at,
        latency_ms=health.latency_ms,
    )


def create_admin_hermes_router(
    manager: HermesManager | None, *, admin_token: str | None
) -> APIRouter:
    guard = AdminTokenGuard(admin_token)
    router = APIRouter(
        prefix="/api/v1/admin/hermes",
        tags=["admin-hermes"],
        dependencies=[Depends(guard)],
    )

    @router.get("/status", response_model=HermesStatusResponse)
    async def status() -> HermesStatusResponse:
        if manager is None:
            return HermesStatusResponse(
                enabled=False,
                health=HermesHealthItem(status="disabled"),
                limits={},
                usage=[],
                recent_runs=[],
            )
        config = await manager.aconfig()
        if config.enabled:
            await manager.probe()
        state = manager.state()
        usage = [
            HermesUsageItem(
                user_id=user_id,
                daily_count=state.daily_counts.get(user_id, 0),
                active_count=state.active_counts.get(user_id, 0),
            )
            for user_id in sorted(set(state.daily_counts) | set(state.active_counts))
        ]
        runs = [HermesRecentRunItem(**_clean_run(entry)) for entry in state.recent_runs[-10:]]
        return HermesStatusResponse(
            enabled=config.enabled,
            health=_health_item(manager),
            limits={
                "daily_run_limit": config.daily_run_limit,
                "max_concurrent_runs": config.max_concurrent_runs,
                "run_max_seconds": config.run_max_seconds,
                "approval_wait_seconds": config.approval_wait_seconds,
            },
            usage=usage,
            recent_runs=runs,
        )

    return router


def _clean_run(entry: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "run_id",
        "user_id",
        "status",
        "reason_code",
        "instruction_chars",
        "output_chars",
        "waited_for_approval",
        "timeout_seconds",
    }
    return {key: entry.get(key) for key in allowed if key in entry}
