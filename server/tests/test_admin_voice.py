"""M2 判卷 Admin 入口：延迟报告查看与统计窗口重置。"""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.admin_voice import create_admin_voice_router
from app.voice.metrics import VoiceLatencyMetrics

AUTH = {"Authorization": "Bearer test-admin-token"}


def _client_app(*, with_reset: bool = True) -> FastAPI:
    metrics = VoiceLatencyMetrics()

    def report() -> dict[str, object]:
        return metrics.snapshot()

    def reset() -> dict[str, object]:
        metrics.clear()
        return metrics.snapshot()

    app = FastAPI()
    app.include_router(
        create_admin_voice_router(
            report, reset if with_reset else None, admin_token="test-admin-token"
        )
    )
    return app


async def test_latency_report_requires_admin_token() -> None:
    app = _client_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/admin/voice/latency")).status_code == 401


async def test_latency_report_and_reset() -> None:
    from app.voice.metrics import VoiceLatencySample

    metrics = VoiceLatencyMetrics()

    def report() -> dict[str, object]:
        return metrics.snapshot()

    def reset() -> dict[str, object]:
        metrics.clear()
        return metrics.snapshot()

    app = FastAPI()
    app.include_router(
        create_admin_voice_router(report, reset, admin_token="test-admin-token")
    )
    metrics.record(
        VoiceLatencySample(
            asr_ms=100, first_token_ms=200, first_audio_ms=1500, total_ms=3000
        )
    )
    metrics.record_interrupt(120)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        report_response = await client.get("/api/v1/admin/voice/latency", headers=AUTH)
        reset_response = await client.post("/api/v1/admin/voice/latency/reset", headers=AUTH)
    body = report_response.json()
    assert report_response.status_code == 200
    assert body["count"] == 1
    assert body["first_audio_ms"]["p90"] == 1500
    assert body["interrupt_ms"]["count"] == 1
    assert body["acceptance"]["overall_pass"] is False  # 样本不足 20 不通过
    assert reset_response.json()["count"] == 0


async def test_reset_endpoint_absent_without_resetter() -> None:
    app = _client_app(with_reset=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/admin/voice/latency/reset", headers=AUTH)
    assert response.status_code in (404, 405)
