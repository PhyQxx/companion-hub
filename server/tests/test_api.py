from httpx import ASGITransport, AsyncClient

from app.main import app


async def test_health() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


async def test_protocol_metadata() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/meta/protocol")
    assert response.status_code == 200
    assert response.json()["supported_protocol_versions"] == [1]


async def test_builtin_adapter_metadata() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/meta/adapters")
    assert response.status_code == 200
    assert {manifest["adapter_id"] for manifest in response.json()["adapters"]} == {
        "builtin.mock_text_input",
        "builtin.mock_ephemeral_sensor",
        "builtin.mock_text_output",
        "builtin.mock_streaming_output",
    }
