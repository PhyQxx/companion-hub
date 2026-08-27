from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image

from app.api.admin_avatar import create_admin_avatar_router
from app.api.avatar import create_avatar_router
from app.auth import AuthService
from app.avatar import AvatarAssetImporter, AvatarStore
from app.db import (
    AvatarPackRecord,
    Base,
    Database,
    PersonaVersionRecord,
    create_database,
)
from app.persona import PersonaConfig, PersonaStore


@pytest.fixture
def tmp_db(tmp_path: Any) -> Database:
    db = create_database(f"sqlite+aiosqlite:///{tmp_path / 'test_avatar.db'}")
    return db


@pytest.fixture
async def database(tmp_db: Database) -> Database:
    async with tmp_db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with tmp_db.sessions.begin() as session:
        session.add(
            PersonaVersionRecord(
                id=1,
                status="published",
                content=PersonaConfig().model_dump(mode="json"),
                content_hash="test-persona",
                created_by="test",
            )
        )
    return tmp_db


@pytest.fixture
def store(database: Database) -> AvatarStore:
    return AvatarStore(database)


class TestAvatarStore:
    async def test_load_builtin_packs(self, store: AvatarStore) -> None:
        count = await store.load_builtin_packs()
        assert count == 2
        packs = await store.list_packs()
        assert len(packs) == 2
        ids = {p.id for p in packs}
        assert ids == {"warm-daily", "light-core"}

    async def test_load_builtin_packs_idempotent(self, store: AvatarStore) -> None:
        await store.load_builtin_packs()
        count = await store.load_builtin_packs()
        assert count == 0

    async def test_load_builtin_packs_refreshes_bundled_assets(
        self, database: Database, store: AvatarStore
    ) -> None:
        await store.load_builtin_packs()
        async with database.sessions.begin() as session:
            pack = await session.get(AvatarPackRecord, "warm-daily")
            assert pack is not None
            pack.content_hash = "builtin-warm-daily-v1"
            pack.manifest = {}

        assert await store.load_builtin_packs() == 0
        refreshed = await store.get_pack("warm-daily")
        assert refreshed is not None
        assert refreshed.content_hash == "builtin-warm-daily-v2"
        assert refreshed.manifest["assets"]["emotions"]["happy"].endswith("happy.png")

    async def test_create_instance(self, store: AvatarStore) -> None:
        await store.load_builtin_packs()
        instance = await store.create_instance("warm-daily", "我的Aria")
        assert instance.pack_id == "warm-daily"
        assert instance.name == "我的Aria"
        assert instance.status == "active"

    async def test_create_instance_unknown_pack(self, store: AvatarStore) -> None:
        with pytest.raises(LookupError):
            await store.create_instance("unknown", "x")

    async def test_get_instance(self, store: AvatarStore) -> None:
        await store.load_builtin_packs()
        created = await store.create_instance("warm-daily", "test")
        fetched = await store.get_instance(created.id)
        assert fetched is not None
        assert fetched.id == created.id

    async def test_update_instance(self, store: AvatarStore) -> None:
        await store.load_builtin_packs()
        created = await store.create_instance("warm-daily", "old")
        updated = await store.update_instance(created.id, name="new", status="archived")
        assert updated is not None
        assert updated.name == "new"
        assert updated.status == "archived"
        assert updated.version == 2

    async def test_rejects_unknown_customization_slot(self, store: AvatarStore) -> None:
        await store.load_builtin_packs()
        with pytest.raises(ValueError, match="unsupported customization slots"):
            await store.create_instance("warm-daily", "custom", customization={"hair": "pink"})

    async def test_delete_instance(self, store: AvatarStore) -> None:
        await store.load_builtin_packs()
        created = await store.create_instance("warm-daily", "del")
        ok = await store.delete_instance(created.id)
        assert ok is True
        after = await store.get_instance(created.id)
        assert after is None

    async def test_bind_and_default(self, store: AvatarStore) -> None:
        await store.load_builtin_packs()
        instance = await store.create_instance("warm-daily", "default")
        ok = await store.bind_to_persona(1, instance.id, is_default=True)
        assert ok is True

        default = await store.get_default_for_persona(1)
        assert default is not None
        assert default.id == instance.id

    async def test_switching_existing_binding_keeps_one_default(self, store: AvatarStore) -> None:
        await store.load_builtin_packs()
        first = await store.create_instance("warm-daily", "first")
        second = await store.create_instance("light-core", "second")
        await store.bind_to_persona(1, first.id, is_default=True)
        await store.bind_to_persona(1, second.id)
        await store.bind_to_persona(1, second.id, is_default=True)

        bindings = await store.list_bindings(persona_id=1)
        assert [item.avatar.id for item in bindings if item.is_default] == [second.id]

    async def test_default_instance_cannot_be_deleted(self, store: AvatarStore) -> None:
        await store.load_builtin_packs()
        instance = await store.create_instance("warm-daily", "default")
        await store.bind_to_persona(1, instance.id, is_default=True)

        with pytest.raises(ValueError, match="default avatar cannot be deleted"):
            await store.delete_instance(instance.id)

    async def test_unbind(self, store: AvatarStore) -> None:
        await store.load_builtin_packs()
        instance = await store.create_instance("warm-daily", "x")
        await store.bind_to_persona(1, instance.id, is_default=True)
        ok = await store.unbind_from_persona(1, instance.id)
        assert ok is True
        default = await store.get_default_for_persona(1)
        assert default is None

    async def test_list_bindings(self, store: AvatarStore) -> None:
        await store.load_builtin_packs()
        i1 = await store.create_instance("warm-daily", "a")
        i2 = await store.create_instance("light-core", "b")
        await store.bind_to_persona(1, i1.id)
        await store.bind_to_persona(1, i2.id)
        bound = await store.list_bindings_for_persona(1)
        assert len(bound) == 2

    async def test_list_instances_filter(self, store: AvatarStore) -> None:
        await store.load_builtin_packs()
        await store.create_instance("warm-daily", "active1")
        i2 = await store.create_instance("warm-daily", "active2")
        await store.update_instance(i2.id, status="archived")
        active = await store.list_instances(status="active")
        assert len(active) == 1
        assert active[0].name == "active1"


async def test_admin_avatar_api_runs_instance_binding_lifecycle(
    database: Database, store: AvatarStore
) -> None:
    await store.load_builtin_packs()
    app = FastAPI()
    app.include_router(create_admin_avatar_router(store, admin_token="test-admin-token"))
    headers = {"Authorization": "Bearer test-admin-token"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        packs = await client.get("/api/v1/admin/avatars/packs", headers=headers)
        created = await client.post(
            "/api/v1/admin/avatars/instances",
            headers=headers,
            json={"pack_id": "light-core", "name": "My Core", "customization": {}},
        )
        instance_id = created.json()["id"]
        bound = await client.post(
            f"/api/v1/admin/avatars/instances/{instance_id}/bind",
            headers=headers,
            json={"persona_id": 1, "is_default": True},
        )
        bindings = await client.get("/api/v1/admin/avatars/bindings?persona_id=1", headers=headers)
        protected = await client.delete(
            f"/api/v1/admin/avatars/instances/{instance_id}", headers=headers
        )
        unbound = await client.delete(
            f"/api/v1/admin/avatars/instances/{instance_id}/bindings/1", headers=headers
        )
        deleted = await client.delete(
            f"/api/v1/admin/avatars/instances/{instance_id}", headers=headers
        )

    assert packs.status_code == 200
    assert packs.json()[0]["manifest"]
    assert created.status_code == 201
    assert bound.json() == {"bound": True}
    assert bindings.json()[0]["is_default"] is True
    assert protected.status_code == 409
    assert unbound.json() == {"unbound": True}
    assert deleted.json() == {"deleted": True}


def _png_bytes() -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", (96, 128), (82, 109, 245))
    image.save(output, format="JPEG", exif=Image.Exif())
    return output.getvalue()


def _live2d_zip(*, traversal: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        model = {
            "Version": 3,
            "FileReferences": {
                "Moc": "model.moc3",
                "Textures": ["textures/texture_00.png"],
            },
        }
        archive.writestr("model/aria.model3.json", json.dumps(model))
        archive.writestr("model/model.moc3", b"test-moc")
        archive.writestr("model/textures/texture_00.png", _png_bytes())
        if traversal:
            archive.writestr("../escape.js", b"alert(1)")
    return output.getvalue()


def _live2d_distribution_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for variant in ("hiyori_free", "hiyori_pro"):
            runtime = f"{variant}/runtime"
            model = {
                "Version": 3,
                "FileReferences": {
                    "Moc": f"{variant}.moc3",
                    "Textures": [f"{variant}.2048/texture_00.png"],
                },
            }
            archive.writestr(f"{variant}/{variant}.cmo3", b"authoring-source")
            archive.writestr(f"{variant}/{variant}.can3", b"animation-source")
            archive.writestr(f"{variant}/ReadMe.txt", b"license")
            archive.writestr(f"{runtime}/{variant}.model3.json", json.dumps(model))
            archive.writestr(f"{runtime}/{variant}.moc3", b"test-moc")
            archive.writestr(f"{runtime}/{variant}.2048/texture_00.png", _png_bytes())
    return output.getvalue()


async def test_static_import_sanitizes_and_installs_avatar(
    tmp_path: Any,
    store: AvatarStore,
) -> None:
    importer = AvatarAssetImporter(tmp_path / "avatar-assets", store)
    result = await importer.import_static(
        _png_bytes(),
        name="自定义立绘",
        rights_confirmed=True,
    )

    assert result.pack.engine == "static"
    assert result.instance.pack_id == result.pack.id
    url = result.pack.manifest["assets"]["thumbnail"]
    stored = tmp_path / "avatar-assets" / url.removeprefix("/api/v1/avatar-user-assets/")
    assert stored.is_file()
    with Image.open(stored) as image:
        assert image.format == "PNG"
        assert image.getexif() == {}


async def test_live2d_import_validates_references_and_rejects_unsafe_archive(
    tmp_path: Any,
    store: AvatarStore,
) -> None:
    importer = AvatarAssetImporter(tmp_path / "avatar-assets", store)
    result = await importer.import_live2d(
        _live2d_zip(),
        name="Aria Live2D",
        rights_confirmed=True,
    )
    assert result.pack.engine == "live2d"
    assert result.pack.manifest["assets"]["model"].endswith("model/aria.model3.json")

    with pytest.raises(ValueError, match="不安全路径"):
        await importer.import_live2d(
            _live2d_zip(traversal=True),
            name="Unsafe",
            rights_confirmed=True,
        )


async def test_live2d_distribution_package_prefers_pro_and_ignores_authoring_files(
    tmp_path: Any,
    store: AvatarStore,
) -> None:
    importer = AvatarAssetImporter(tmp_path / "avatar-assets", store)
    result = await importer.import_live2d(
        _live2d_distribution_zip(),
        name="Hiyori",
        rights_confirmed=True,
    )

    source = result.pack.manifest["source_package"]
    assert source["selected_model"] == "hiyori_pro/runtime/hiyori_pro.model3.json"
    assert len(source["models_detected"]) == 2
    assert source["ignored_files"] == 9
    stored_root = tmp_path / "avatar-assets" / "live2d"
    assert not list(stored_root.rglob("*.cmo3"))
    assert not list(stored_root.rglob("*.can3"))
    assert list(stored_root.rglob("hiyori_pro.model3.json"))
    assert not list(stored_root.rglob("hiyori_free.model3.json"))


async def test_admin_static_import_endpoint_accepts_multipart(
    tmp_path: Any,
    store: AvatarStore,
) -> None:
    importer = AvatarAssetImporter(tmp_path / "avatar-assets", store)
    app = FastAPI()
    app.include_router(
        create_admin_avatar_router(
            store,
            admin_token="test-admin-token",
            asset_importer=importer,
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/admin/avatars/imports/static",
            headers={"Authorization": "Bearer test-admin-token"},
            data={"name": "上传立绘", "rights_confirmed": "true"},
            files={"file": ("portrait.jpg", _png_bytes(), "image/jpeg")},
        )
    assert response.status_code == 201
    assert response.json()["pack"]["engine"] == "static"


async def test_chat_avatar_api_lists_and_switches_without_changing_persona(
    database: Database,
    store: AvatarStore,
) -> None:
    persona_store = PersonaStore(database)
    persona = await persona_store.load()
    await store.load_builtin_packs()
    first = await store.create_instance("warm-daily", "日常")
    second = await store.create_instance("light-core", "光核")
    await store.bind_to_persona(persona.version, first.id, is_default=True)
    auth = AuthService(database)
    auth_session = await auth.setup(
        display_name="Owner",
        password="correct horse battery staple",
    )
    app = FastAPI()
    app.include_router(create_avatar_router(store, persona_store, auth))
    headers = {"Authorization": f"Bearer {auth_session.access_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        before = await client.get("/api/v1/avatars", headers=headers)
        switched = await client.put(
            "/api/v1/avatars/current",
            headers=headers,
            json={"instance_id": str(second.id)},
        )
        after = await client.get("/api/v1/avatars", headers=headers)

    assert before.status_code == 200, before.text
    assert len(before.json()) == 2
    assert switched.status_code == 200 and switched.json()["is_current"] is True
    assert [item["instance_id"] for item in after.json() if item["is_current"]] == [str(second.id)]
    assert (await persona_store.refresh()).version == persona.version
