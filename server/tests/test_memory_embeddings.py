"""SEMB（docs/09 §2）语义嵌入：配置校验、HTTP provider、探测回落与换入。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config.models import EmbeddingsConfig
from app.memory import (
    HashingEmbeddingProvider,
    HttpEmbeddingProvider,
    build_embedding_provider,
    probe_embedding_provider,
)


def _provider(
    transport: httpx.AsyncBaseTransport, *, dimension: int = 16, api_key: str | None = None
) -> HttpEmbeddingProvider:
    return HttpEmbeddingProvider(
        base_url="http://127.0.0.1:1234/v1",
        model="text-embedding-test",
        dimension=dimension,
        api_key=api_key,
        timeout_ms=2_000,
        transport=transport,
    )


def _ok_transport(
    dimension: int = 16,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [0.1] * dimension}
                    for index in range(len(body["input"]))
                ],
            },
        )

    return httpx.MockTransport(handler), requests


class _StaticSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, reference: str) -> str:
        return self._values[reference]


def test_embeddings_config_requires_fields_when_enabled() -> None:
    assert EmbeddingsConfig().enabled is False

    with pytest.raises(ValueError):
        EmbeddingsConfig(enabled=True)
    with pytest.raises(ValueError):
        EmbeddingsConfig(
            enabled=True,
            base_url="http://127.0.0.1:1234/v1",
            model="text-embedding-test",
        )
    with pytest.raises(ValueError):
        EmbeddingsConfig(
            enabled=True,
            base_url="http://127.0.0.1:1234/v1",
            model="text-embedding-test",
            dimension=1024,
            secret_ref="env:KEY",
            secret_value="inline",
        )
    valid = EmbeddingsConfig(
        enabled=True,
        base_url="http://127.0.0.1:1234/v1",
        model="text-embedding-test",
        dimension=1024,
    )
    assert valid.timeout_ms == 5_000


async def test_http_embedding_provider_posts_and_validates() -> None:
    transport, requests = _ok_transport(dimension=16)
    provider = _provider(transport, api_key="sk-test")

    vectors = await provider.embed(["你好", "世界"])

    assert len(vectors) == 2
    assert vectors[0] == [0.1] * 16
    assert provider.version == "text-embedding-test/1"
    assert provider.endpoint == "http://127.0.0.1:1234/v1/embeddings"
    assert len(requests) == 1
    sent = json.loads(requests[0].content.decode("utf-8"))
    assert sent == {"model": "text-embedding-test", "input": ["你好", "世界"]}
    assert requests[0].headers["Authorization"] == "Bearer sk-test"
    assert await provider.embed([]) == []


async def test_http_embedding_provider_rejects_dimension_mismatch() -> None:
    transport, _ = _ok_transport(dimension=8)
    provider = _provider(transport, dimension=4)

    with pytest.raises(ValueError, match="dimension mismatch"):
        await provider.embed(["你好"])


async def test_http_embedding_provider_raises_on_http_error() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    provider = _provider(transport)

    with pytest.raises(httpx.HTTPStatusError):
        await provider.embed(["你好"])


def test_build_embedding_provider_disabled_returns_none() -> None:
    assert build_embedding_provider(EmbeddingsConfig(), _StaticSecrets({})) is None


def test_build_embedding_provider_resolves_secret_ref() -> None:
    config = EmbeddingsConfig(
        enabled=True,
        base_url="http://127.0.0.1:1234/v1",
        model="text-embedding-test",
        dimension=16,
        secret_ref="env:EMBED_KEY",
    )
    provider = build_embedding_provider(config, _StaticSecrets({"env:EMBED_KEY": "kv"}))
    assert provider is not None
    assert provider.model_name == "text-embedding-test"

    config_value = config.model_copy(
        update={"secret_ref": None, "secret_value": "inline"}
    )
    inline = build_embedding_provider(config_value, _StaticSecrets({}))
    assert inline is not None


async def test_probe_accepts_healthy_provider_and_rejects_broken() -> None:
    ok_transport, _ = _ok_transport(dimension=16)
    assert await probe_embedding_provider(_provider(ok_transport)) is True

    broken = httpx.MockTransport(lambda request: httpx.Response(500))
    assert await probe_embedding_provider(_provider(broken)) is False


def test_memory_store_swaps_embedding_provider() -> None:
    from app.memory import MemoryStore

    class _FakeDatabase:
        pass

    store = MemoryStore(_FakeDatabase())  # type: ignore[arg-type]
    assert isinstance(store.embedding_provider, HashingEmbeddingProvider)

    transport, _ = _ok_transport(dimension=16)
    store.set_embedding_provider(_provider(transport))
    assert isinstance(store.embedding_provider, HttpEmbeddingProvider)
    assert store.embedding_provider.version == "text-embedding-test/1"
