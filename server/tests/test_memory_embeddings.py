"""SEMB（docs/09 §2）语义嵌入：配置校验、HTTP provider、探测回落与换入。

方案 A2（嵌入版本回填）的门控/续跑/收敛测试也在此文件。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx
import pytest

from app.config.models import EmbeddingsConfig
from app.db import Database
from app.memory import (
    HashingEmbeddingProvider,
    HttpEmbeddingProvider,
    build_embedding_provider,
    probe_embedding_provider,
)


@pytest.fixture(params=["sqlite", "postgresql"])
async def database(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Database]:
    from test_run_cancel_fence import prepared

    storage = await prepared(request.param, tmp_path)
    try:
        yield storage.database
    finally:
        await storage.close()


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

    config_value = config.model_copy(update={"secret_ref": None, "secret_value": "inline"})
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


class _FakeProvider:
    """确定性测试 provider：向量与内容长度相关，版本与哈希默认互不兼容。"""

    def __init__(self, *, version: str = "fake-embed/9", dimension: int = 8) -> None:
        self._version = version
        self._dimension = dimension

    @property
    def model_name(self) -> str:
        return "fake.model"

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def version(self) -> str:
        return self._version

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(len(text) % 7 + 1)] * self._dimension for text in texts]


async def test_attach_defers_swap_and_backfill_completes(database: Database) -> None:
    """方案 A2：库内存在旧版本嵌入时换版被推迟，回填收敛后才换入。"""
    from test_goal_delivery_runs import seed
    from test_memory import candidate

    from app.memory import MemoryRetriever, MemoryStore
    from app.memory.embedding_backfill import attach_embedding_provider
    from app.schemas import PrivacyLevel

    owner, _, _ = await seed(database)
    store = MemoryStore(database)
    content = "fixture embedding backfill fact"
    entry = await store.add(candidate(content), user_id=owner)
    await store.add(candidate("fixture embedding backfill second"), user_id=owner)
    async with database.sessions.begin() as session:
        from app.db import MemoryRecord

        row = await session.get_one(MemoryRecord, entry.id)
        row.embedding_version = None  # 模拟历史空版本行
    pending = _FakeProvider()
    assert await store.count_embedding_mismatch(version=pending.version) == 2

    service = await attach_embedding_provider(
        store, pending, interval_seconds=3_600.0, batch_size=1
    )
    assert service is not None
    try:
        # 门控生效：旧 provider 仍在服务，目标版本进入待换入状态
        assert isinstance(store.embedding_provider, HashingEmbeddingProvider)
        assert store.pending_embedding_version == pending.version
        assert store.embedding_stale_rows == 2
        # 回填期间检索不阻塞：仍可通过词法/精确通道命中
        hits = await MemoryRetriever(store).retrieve(
            content, user_id=owner, privacy_level=PrivacyLevel.L1
        )
        assert hits.hits and hits.hits[0].memory.id == entry.id
        # 断点续跑：先手动重算一批（1 行），剩余 1 行由循环收敛
        assert await store.reembed_stale_batch(provider=pending, limit=1) == 1
        assert await store.count_embedding_mismatch(version=pending.version) == 1
        assert await service.run_once() is False
        assert store.embedding_provider.version == "char-ngram-hash/1"  # 未换入
        assert await service.run_once() is True
        assert store.embedding_provider.version == pending.version  # 已换入
        assert store.pending_embedding_version is None
        assert store.embedding_stale_rows == 0
    finally:
        await service.stop()


async def test_attach_swaps_immediately_when_no_stale(database: Database) -> None:
    """无落后行时门控直接换入，行为与原实现一致。"""
    from test_goal_delivery_runs import seed
    from test_memory import candidate

    from app.memory import MemoryStore
    from app.memory.embedding_backfill import attach_embedding_provider

    owner, _, _ = await seed(database)
    store = MemoryStore(database)
    await store.add(candidate("fixture no stale fact"), user_id=owner)
    fresh = _FakeProvider()
    # 先把全库重算到目标版本，模拟"上轮回填已完成"的收敛态
    await store.reembed_stale_batch(provider=fresh, limit=10)
    assert await store.count_embedding_mismatch(version=fresh.version) == 0
    service = await attach_embedding_provider(store, fresh, interval_seconds=3_600.0)
    assert service is None
    assert store.embedding_provider is fresh
    assert store.embedding_stale_rows == 0


async def test_attach_defers_without_backfill_when_disabled(database: Database) -> None:
    """开关关闭（ARIA_MEMORY_EMBED_BACKFILL=0）：推迟换版且不启动回填。"""
    from test_goal_delivery_runs import seed
    from test_memory import candidate

    from app.memory import MemoryStore
    from app.memory.embedding_backfill import attach_embedding_provider

    owner, _, _ = await seed(database)
    store = MemoryStore(database)
    await store.add(candidate("fixture disabled backfill fact"), user_id=owner)
    pending = _FakeProvider()
    service = await attach_embedding_provider(store, pending, backfill_enabled=False)
    assert service is None
    assert isinstance(store.embedding_provider, HashingEmbeddingProvider)
    assert store.pending_embedding_version == pending.version
    assert store.embedding_stale_rows == 1
    # 关闭回填后剩余行保持不变，切换维持推迟
    assert await store.count_embedding_mismatch(version=pending.version) == 1
