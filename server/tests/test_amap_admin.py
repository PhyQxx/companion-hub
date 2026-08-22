from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import DatabaseConfigStore
from app.db import Base, Database, create_database
from app.main import create_app
from app.tools import ToolLedger, ToolResult


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


@pytest.fixture(autouse=True)
def clear_ledger() -> None:
    ToolLedger().clear()


def _amap_ok_transport(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v3/geocode/geo":
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "geocodes": [
                    {
                        "formatted_address": "山东省济南市",
                        "adcode": "370100",
                        "citycode": "0531",
                        "location": "117.000000,36.650000",
                    }
                ],
            },
        )
    if path == "/v3/weather/weatherInfo":
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "lives": [
                    {
                        "city": "济南市",
                        "weather": "多云",
                        "temperature": "29",
                        "reporttime": "2026-08-21 11:00:00",
                    }
                ],
            },
        )
    return httpx.Response(200, json={"status": "1", "infocode": "10000"})


def _amap_auth_error_transport(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": "0", "infocode": "10001", "info": "INVALID_USER_KEY"},
    )


@pytest.fixture
def bootstrap(tmp_path: Path) -> Path:
    path = tmp_path / "hub.yaml"
    path.write_text(
        """
schema_version: 1
models:
  cloud:
    provider: openai_compatible
    model: test-model
    base_url: https://models.example/v1
    secret_ref: env:MODEL_API_KEY
    runs_local: false
    max_privacy_level: L1
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
  local:
    provider: openai_compatible
    model: local-model
    base_url: http://127.0.0.1:11434/v1
    runs_local: true
    max_privacy_level: L2
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
routes:
  dialogue: {primary: cloud}
  utility: {primary: cloud}
  private: {primary: local}
""",
        encoding="utf-8",
    )
    return path


class TestToolLedger:
    def test_record_and_snapshot(self) -> None:
        ledger = ToolLedger()
        ledger.record(
            ToolResult(
                ok=True,
                tool_name="get_weather",
                provider="amap",
                latency_ms=120.0,
                cache_hit=True,
                location_source="default_city",
                data={"current": {}, "forecast": [{}, {}]},
            )
        )
        snapshot = ledger.snapshot()
        assert len(snapshot) == 1
        assert snapshot[0].tool_name == "get_weather"
        assert snapshot[0].ok is True
        assert snapshot[0].cache_hit is True
        assert snapshot[0].result_count == 3  # 1 current + 2 forecast

    def test_max_entries_200(self) -> None:
        ledger = ToolLedger()
        for i in range(250):
            ledger.record(
                ToolResult(
                    ok=True,
                    tool_name="get_weather",
                    provider="amap",
                    latency_ms=float(i),
                    data={},
                )
            )
        assert len(ledger.snapshot()) == 200

    def test_metrics_empty(self) -> None:
        ledger = ToolLedger()
        metrics = ledger.metrics()
        assert metrics["total_calls"] == 0
        assert metrics["success_rate"] is None

    def test_metrics_aggregation(self) -> None:
        ledger = ToolLedger()
        for i in range(10):
            ledger.record(
                ToolResult(
                    ok=i < 8,
                    tool_name="get_weather",
                    provider="amap",
                    latency_ms=float(i * 10),
                    cache_hit=i < 3,
                    reason_code="provider_timeout" if i >= 8 else None,
                    data={},
                )
            )
        metrics = ledger.metrics()
        assert metrics["total_calls"] == 10
        assert metrics["success_rate"] == 0.8
        assert metrics["cache_hit_rate"] == 0.3
        assert metrics["failures"].get("provider_timeout") == 2
        assert metrics["p50_latency_ms"] is not None
        assert metrics["p90_latency_ms"] is not None

    def test_nearby_result_count(self) -> None:
        ledger = ToolLedger()
        ledger.record(
            ToolResult(
                ok=True,
                tool_name="search_nearby",
                provider="amap",
                latency_ms=150.0,
                data={"results": [{}, {}, {}]},
            )
        )
        assert ledger.snapshot()[0].result_count == 3

    def test_route_result_count(self) -> None:
        ledger = ToolLedger()
        ledger.record(
            ToolResult(
                ok=True,
                tool_name="plan_route",
                provider="amap",
                latency_ms=200.0,
                data={"steps": [{}, {}]},
            )
        )
        assert ledger.snapshot()[0].result_count == 2


class TestAmapAdminApi:
    async def test_amap_connection_ok(
        self,
        database: Database,
        bootstrap: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = DatabaseConfigStore(database, bootstrap)
        app = create_app(
            database,
            config_store=store,
            watch_config=False,
            admin_token="test-admin-token",
        )
        headers = {"Authorization": "Bearer test-admin-token"}

        monkeypatch.setattr(
            "app.api.admin_config.AmapProvider",
            lambda api_key, **kwargs: _mock_provider(api_key, _amap_ok_transport),
        )

        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/admin/config/tools/amap/test",
                headers=headers,
                json={
                    "base_url": "https://restapi.amap.com",
                    "secret_value": "test-key",
                    "timeout_ms": 3500,
                    "max_retries": 1,
                    "max_concurrency": 2,
                    "requests_per_minute": 30,
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["message"] == "全部通过"
        assert len(body["steps"]) == 3
        assert all(step["ok"] for step in body["steps"])

    async def test_amap_connection_auth_fail(
        self,
        database: Database,
        bootstrap: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = DatabaseConfigStore(database, bootstrap)
        app = create_app(
            database,
            config_store=store,
            watch_config=False,
            admin_token="test-admin-token",
        )
        headers = {"Authorization": "Bearer test-admin-token"}

        monkeypatch.setattr(
            "app.api.admin_config.AmapProvider",
            lambda api_key, **kwargs: _mock_provider(api_key, _amap_auth_error_transport),
        )

        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/admin/config/tools/amap/test",
                headers=headers,
                json={
                    "base_url": "https://restapi.amap.com",
                    "secret_value": "bad-key",
                    "timeout_ms": 3500,
                    "max_retries": 1,
                    "max_concurrency": 2,
                    "requests_per_minute": 30,
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert any(step["name"] == "geocode" and not step["ok"] for step in body["steps"])

    async def test_amap_connection_missing_key(
        self,
        database: Database,
        bootstrap: Path,
    ) -> None:
        store = DatabaseConfigStore(database, bootstrap)
        app = create_app(
            database,
            config_store=store,
            watch_config=False,
            admin_token="test-admin-token",
        )
        headers = {"Authorization": "Bearer test-admin-token"}

        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/admin/config/tools/amap/test",
                headers=headers,
                json={
                    "base_url": "https://restapi.amap.com",
                    "secret_value": None,
                    "secret_ref": None,
                    "timeout_ms": 3500,
                    "max_retries": 1,
                    "max_concurrency": 2,
                    "requests_per_minute": 30,
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["steps"][0]["name"] == "key_resolve"
        assert body["steps"][0]["ok"] is False

    async def test_amap_metrics_and_ledger(
        self,
        database: Database,
        bootstrap: Path,
    ) -> None:
        store = DatabaseConfigStore(database, bootstrap)
        app = create_app(
            database,
            config_store=store,
            watch_config=False,
            admin_token="test-admin-token",
        )
        headers = {"Authorization": "Bearer test-admin-token"}

        # 预先填充台账
        ledger = ToolLedger()
        ledger.record(
            ToolResult(
                ok=True,
                tool_name="get_weather",
                provider="amap",
                latency_ms=100.0,
                cache_hit=True,
                data={"current": {}, "forecast": []},
            )
        )
        ledger.record(
            ToolResult(
                ok=False,
                tool_name="search_nearby",
                provider="amap",
                latency_ms=200.0,
                reason_code="provider_timeout",
                data={},
            )
        )

        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            metrics_resp = await client.get(
                "/api/v1/admin/config/tools/amap/metrics",
                headers=headers,
            )
            ledger_resp = await client.get(
                "/api/v1/admin/config/tools/amap/ledger",
                headers=headers,
            )

        assert metrics_resp.status_code == 200
        metrics = metrics_resp.json()
        assert metrics["total_calls"] == 2
        assert metrics["success_rate"] == 0.5
        assert metrics["cache_hit_rate"] == 0.5
        assert metrics["failures"].get("provider_timeout") == 1

        assert ledger_resp.status_code == 200
        ledger_data = ledger_resp.json()
        assert len(ledger_data) == 2
        assert ledger_data[0]["tool_name"] == "search_nearby"
        assert ledger_data[1]["tool_name"] == "get_weather"

    async def test_amap_metrics_empty(self, database: Database, bootstrap: Path) -> None:
        store = DatabaseConfigStore(database, bootstrap)
        app = create_app(
            database,
            config_store=store,
            watch_config=False,
            admin_token="test-admin-token",
        )
        headers = {"Authorization": "Bearer test-admin-token"}

        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/admin/config/tools/amap/metrics",
                headers=headers,
            )

        assert response.status_code == 200
        metrics = response.json()
        assert metrics["total_calls"] == 0
        assert metrics["success_rate"] is None


def _mock_provider(api_key: str, transport) -> object:
    """构造一个使用给定 transport 的 AmapProvider, 用于 monkeypatch。"""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        base_url="https://restapi.amap.com",
    )
    from app.tools import AmapProvider

    return AmapProvider(api_key, client=client)
