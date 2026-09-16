"""M2 语音延迟判卷的 Admin 入口：查看滑动窗口报告与重置统计窗口。"""

from collections.abc import Callable

from fastapi import APIRouter, Depends

from .admin_config import AdminTokenGuard


def create_admin_voice_router(
    latency_report: Callable[[], dict[str, object]],
    latency_reset: Callable[[], dict[str, object]] | None,
    *,
    admin_token: str | None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/voice",
        tags=["admin-voice"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("/latency")
    async def voice_latency() -> dict[str, object]:
        """M2 判卷报告：完成回合数、首音频/打断 P50/P90 与总体判定。"""
        return latency_report()

    if latency_reset is not None:

        @router.post("/latency/reset")
        async def reset_voice_latency() -> dict[str, object]:
            """重置统计窗口（正式计样前使用，旧样本全部丢弃）。"""
            return latency_reset()

    return router
