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


async def test_latency_report_includes_speculation_feasibility() -> None:
    """P2 前置测量：稳定停顿与 partial 匹配关系进入判卷报表。"""
    from app.voice.metrics import VoiceLatencySample

    metrics = VoiceLatencyMetrics()
    metrics.record(
        VoiceLatencySample(
            asr_ms=50,
            first_token_ms=1500,
            first_audio_ms=2500,
            total_ms=3000,
            stable_partial_ms=850,
            partial_match="exact",
        )
    )
    metrics.record(
        VoiceLatencySample(
            asr_ms=60,
            first_token_ms=1600,
            first_audio_ms=2600,
            total_ms=3200,
            stable_partial_ms=250,
            partial_match="prefix",
        )
    )
    metrics.record(
        VoiceLatencySample(
            asr_ms=70, first_token_ms=1700, first_audio_ms=2700, total_ms=3400
        )
    )
    snapshot = metrics.snapshot()
    speculation = snapshot["speculation"]
    assert isinstance(speculation, dict)
    assert speculation["streamed_turns"] == 2
    assert speculation["pause_ge_300ms"] == 1
    assert speculation["pause_ge_600ms"] == 1
    assert speculation["partial_exact_match"] == 1
    assert speculation["partial_prefix"] == 1
    assert speculation["partial_diverged"] == 0
    stable = speculation["stable_partial_ms"]
    assert isinstance(stable, dict)
    assert stable["count"] == 2
