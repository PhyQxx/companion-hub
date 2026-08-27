from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from app.db import AssetRecord, Base, Database, create_database
from app.jobs import AssetStore


@pytest.fixture
def tmp_db(tmp_path: Any) -> Database:
    db = create_database(f"sqlite+aiosqlite:///{tmp_path / 'test_assets.db'}")
    return db


@pytest.fixture
async def database(tmp_db: Database) -> Database:
    async with tmp_db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return tmp_db


@pytest.fixture
def store(database: Database, tmp_path: Any) -> AssetStore:
    return AssetStore(database, tmp_path / "assets")


class TestAssetStore:
    async def test_store_and_get(self, store: AssetStore) -> None:
        data = b"hello world"
        asset = await store.store(data, media_type="text/plain", privacy_level="L1")
        assert asset.byte_size == 11
        assert asset.media_type == "text/plain"
        assert asset.state == "staging"

        fetched = await store.get(asset.id)
        assert fetched is not None
        assert fetched.content_hash == asset.content_hash

    async def test_deduplication_by_hash(self, store: AssetStore) -> None:
        data = b"same content"
        a1 = await store.store(data, media_type="image/png")
        a2 = await store.store(data, media_type="image/png")
        assert a1.id == a2.id

    async def test_commit(self, store: AssetStore) -> None:
        data = b"test"
        asset = await store.store(data, media_type="text/plain")
        ok = await store.commit(asset.id)
        assert ok is True
        after = await store.get(asset.id)
        assert after is not None
        assert after.state == "active"

    async def test_read_bytes(self, store: AssetStore) -> None:
        data = b"payload"
        asset = await store.store(data, media_type="application/octet-stream")
        read_back = await store.read_bytes(asset.id)
        assert read_back == data

    async def test_reference_lifecycle(self, store: AssetStore) -> None:
        data = b"ref-test"
        asset = await store.store(data, media_type="text/plain")
        await store.commit(asset.id)

        ok = await store.add_reference(asset.id, "job", "job-001", "output")
        assert ok is True
        refs = await store.list_references(asset.id)
        assert len(refs) == 1

        ok2 = await store.remove_reference(asset.id, "job", "job-001", "output")
        assert ok2 is True
        refs2 = await store.list_references(asset.id)
        assert len(refs2) == 0

        # 无引用后状态变为 unreferenced
        after = await store.get(asset.id)
        assert after is not None
        assert after.state == "unreferenced"

    async def test_reactivate_on_reference(self, store: AssetStore) -> None:
        data = b"reactivate"
        asset = await store.store(data, media_type="text/plain")
        await store.commit(asset.id)
        await store.add_reference(asset.id, "job", "j1", "out")
        await store.remove_reference(asset.id, "job", "j1", "out")

        # 重新添加引用应激活资产
        ok = await store.add_reference(asset.id, "job", "j2", "out")
        assert ok is True
        after = await store.get(asset.id)
        assert after is not None
        assert after.state == "active"

    async def test_derivation(self, store: AssetStore) -> None:
        parent = await store.store(b"parent", media_type="image/png")
        child = await store.store(b"child", media_type="image/png")
        await store.add_derivation(
            parent.id, child.id, "upscale", model_version="esrgan-v1"
        )

    async def test_gc_staging(self, store: AssetStore) -> None:
        asset = await store.store(b"old", media_type="text/plain")
        count = await store.gc_staging(max_age_hours=-1)
        assert count == 1
        after = await store.get(asset.id)
        assert after is None

    async def test_gc_unreferenced(self, store: AssetStore) -> None:
        data = b"orphan"
        asset = await store.store(data, media_type="text/plain")
        await store.commit(asset.id)
        await store.add_reference(asset.id, "job", "j1", "out")
        await store.remove_reference(asset.id, "job", "j1", "out")

        count = await store.gc_unreferenced(grace_days=-1)
        assert count == 1
        after = await store.get(asset.id)
        assert after is not None
        assert after.state == "deleted"
