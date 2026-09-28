from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.admin_skills import create_admin_skills_router
from app.db import Base, Database, SkillCredentialRecord, create_database
from app.llm import EnvSecretProvider
from app.schemas import PrivacyLevel
from app.skills import SkillDocument, import_skill_zip
from app.skills.connections import (
    SkillConnection,
    SkillConnectionStore,
    SkillHttpClient,
)
from app.skills.credentials import SkillCredentialStore
from app.skills.models import SkillApiManifest, SkillLoginAuth, SkillOperation
from app.skills.runtime import SkillToolProvider, _relevance
from app.skills.store import SkillStore
from app.tools.contracts import ToolContext


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield value


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, body in files.items():
            archive.writestr(path, body)
    return buffer.getvalue()


SKILL_MD = """---
name: pnkx-coupons
description: 查询 PNKX 情侣卡券。用户询问情侣券、卡券时使用。
---
先查询卡券，再向用户说明状态。
"""
API_YAML = """schema_version: 1
connection: pnkx-admin
operations:
  - name: coupon_list
    description: 查询情侣卡券
    method: GET
    path: /coupon/list
    risk: read
    parameters:
      status: {type: string, required: false, location: query}
  - name: coupon_redeem
    description: 核销情侣卡券
    method: POST
    path: /coupon/{coupon_id}/redeem
    risk: confirm
    parameters:
      coupon_id: {type: integer, required: true, location: path}
"""


def test_zip_import_and_path_guards() -> None:
    package = _zip({"pnkx-coupons/SKILL.md": SKILL_MD, "pnkx-coupons/aria-api.yaml": API_YAML})
    document, digest = import_skill_zip(package)
    assert document.name == "pnkx-coupons"
    assert document.api is not None and len(document.api.operations) == 2
    assert len(digest) == 64
    with pytest.raises(ValueError, match="unsafe"):
        import_skill_zip(_zip({"../SKILL.md": SKILL_MD}))
    with pytest.raises(ValueError, match="pnkx_connection_reserved"):
        SkillApiManifest.model_validate(
            {
                "schema_version": 1,
                "connection": "pnkx",
                "operations": [
                    {
                        "name": "ok",
                        "description": "ok",
                        "method": "GET",
                        "path": "/coupon/list",
                        "risk": "read",
                    }
                ],
            }
        )
    with pytest.raises(ValueError, match="relative"):
        SkillDocument.model_validate(
            {
                **document.model_dump(),
                "api": {
                    "schema_version": 1,
                    "connection": "pnkx-admin",
                    "operations": [
                        {
                            "name": "bad",
                            "description": "bad",
                            "method": "GET",
                            "path": "https://evil.example",
                            "risk": "read",
                        }
                    ],
                },
            }
        )


@pytest.mark.asyncio
async def test_catalog_api_and_read_execution(database: Database) -> None:
    store = SkillStore(database)
    app = FastAPI()
    app.include_router(create_admin_skills_router(store, admin_token="admin-secret"))
    package = _zip({"pnkx-coupons/SKILL.md": SKILL_MD, "pnkx-coupons/aria-api.yaml": API_YAML})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as admin:
        assert (await admin.get("/api/v1/admin/skills")).status_code == 401
        headers = {"Authorization": "Bearer admin-secret"}
        imported = await admin.post(
            "/api/v1/admin/skills/import",
            files={"file": ("skill.zip", package, "application/zip")},
            headers=headers,
        )
        assert imported.status_code == 201
        skill_id = imported.json()["id"]
        assert not imported.json()["enabled"]
        assert (await admin.get("/api/v1/admin/skills", headers=headers)).json()[0][
            "name"
        ] == "pnkx-coupons"
        edited = await admin.put(
            f"/api/v1/admin/skills/{skill_id}/api",
            json=imported.json()["api"],
            headers=headers,
        )
        assert edited.status_code == 200
        assert edited.json()["version"] == 2

        requests: list[httpx.Request] = []

        def remote(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"code": 200, "data": [{"name": "双人券"}]})

        connections = SkillConnectionStore(database)
        await connections.put(
            SkillConnection(
                id="pnkx-admin",
                base_url="https://pnkx.example.test",
                auth_type="bearer",
                secret_ref="env:PNKX_TEST_TOKEN",
                allowed_paths=["/coupon/list"],
                enabled=True,
            )
        )
        client = SkillHttpClient(
            connections,
            secrets=EnvSecretProvider({"PNKX_TEST_TOKEN": "secret"}),
            client=httpx.AsyncClient(transport=httpx.MockTransport(remote)),
        )
        provider = SkillToolProvider(store, connections=connections, http_client=client)
        assert not await provider.select("查询情侣卡券", privacy_level=PrivacyLevel.L1)
        enabled = await admin.put(
            f"/api/v1/admin/skills/{skill_id}/enabled",
            json={"enabled": True},
            headers=headers,
        )
        assert enabled.status_code == 200
        assert "先查询卡券" in await provider.guidance("看看情侣卡券")
        assert await provider.select("看看情侣卡券", privacy_level=PrivacyLevel.L1)
        assert not await provider.guidance("今天天气如何")
        preview_app = FastAPI()
        preview_app.include_router(
            create_admin_skills_router(
                store,
                admin_token="admin-secret",
                tool_provider=provider,
            )
        )
        async with AsyncClient(
            transport=ASGITransport(app=preview_app), base_url="http://test"
        ) as preview_client:
            preview = await preview_client.post(
                "/api/v1/admin/skills/preview",
                json={"text": "看看情侣卡券", "privacy_level": "L1"},
                headers=headers,
            )
            assert preview.status_code == 200
            assert preview.json()["tools"] == ["skill.pnkx-coupons.coupon_list"]
        assert (
            await admin.put(
                f"/api/v1/admin/skills/{skill_id}/api",
                json=imported.json()["api"],
                headers=headers,
            )
        ).status_code == 409
        handlers = await provider.select("看看有哪些情侣卡券", privacy_level=PrivacyLevel.L1)
        assert len(handlers) == 1
        assert not await provider.select("看看有哪些情侣卡券", privacy_level=PrivacyLevel.L2)
        result = await handlers[0].execute(
            handlers[0].arguments_model.model_validate({"status": "active"}),
            ToolContext(privacy_level=PrivacyLevel.L1),
        )
        assert result.ok
        assert requests[0].url.path == "/coupon/list"
        assert requests[0].url.params["status"] == "active"
        assert requests[0].headers["Authorization"] == "Bearer secret"
        await admin.put(
            f"/api/v1/admin/skills/{skill_id}/enabled",
            json={"enabled": False},
            headers=headers,
        )
        assert not (
            await handlers[0].execute(
                handlers[0].arguments_model.model_validate({}),
                ToolContext(privacy_level=PrivacyLevel.L1),
            )
        ).ok
        assert len(requests) == 1
        versions = await admin.get(f"/api/v1/admin/skills/{skill_id}/versions", headers=headers)
        assert [item["version"] for item in versions.json()] == [2, 1]
        rolled_back = await admin.post(
            f"/api/v1/admin/skills/{skill_id}/rollback",
            json={"version": 1},
            headers=headers,
        )
        assert rolled_back.status_code == 200
        assert rolled_back.json()["version"] == 3


@pytest.mark.asyncio
async def test_generic_connection_read_revocation_and_run_evidence(database: Database) -> None:
    store = SkillStore(database)
    connections = SkillConnectionStore(database)
    requests: list[httpx.Request] = []

    def remote(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"coupons": ["two"]})

    http_client = SkillHttpClient(
        connections,
        secrets=EnvSecretProvider({"PARTNER_API_TOKEN": "test-token"}),
        client=httpx.AsyncClient(transport=httpx.MockTransport(remote)),
    )
    provider = SkillToolProvider(store, connections=connections, http_client=http_client)
    app = FastAPI()
    app.include_router(
        create_admin_skills_router(
            store, admin_token="admin-secret", tool_provider=provider, connections=connections
        )
    )
    headers = {"Authorization": "Bearer admin-secret"}
    document = SkillDocument.model_validate(
        {
            "name": "partner-coupons",
            "description": "查询情侣卡券",
            "instructions": "询问卡券时读取列表。",
            "api": {
                "schema_version": 1,
                "connection": "partner-system",
                "operations": [
                    {
                        "name": "list_coupons",
                        "description": "查询情侣卡券",
                        "method": "GET",
                        "path": "/coupons/{user_id}",
                        "risk": "read",
                        "parameters": {
                            "user_id": {"type": "string", "required": True, "location": "path"}
                        },
                    }
                ],
            },
        }
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as admin:
        created = await admin.post(
            "/api/v1/admin/skills", json=document.model_dump(), headers=headers
        )
        assert created.status_code == 201
        skill_id = created.json()["id"]
        await admin.put(
            f"/api/v1/admin/skills/{skill_id}/enabled",
            json={"enabled": True},
            headers=headers,
        )
        assert not await provider.select("查询情侣卡券", privacy_level=PrivacyLevel.L1)
        connection = SkillConnection(
            id="partner-system",
            base_url="https://partner.example.test/prod-api",
            auth_type="bearer",
            secret_ref="env:PARTNER_API_TOKEN",
            allowed_paths=["/coupons/{user_id}"],
            enabled=True,
        )
        denied = await admin.put(
            "/api/v1/admin/skills/connections/partner-system",
            json=connection.model_dump(),
        )
        assert denied.status_code == 401
        saved = await admin.put(
            "/api/v1/admin/skills/connections/partner-system",
            json=connection.model_dump(),
            headers=headers,
        )
        assert saved.status_code == 200
        handlers = await provider.select("查询情侣卡券", privacy_level=PrivacyLevel.L1)
        assert len(handlers) == 1
        result = await handlers[0].execute(
            handlers[0].arguments_model.model_validate({"user_id": "user-1"}),
            ToolContext(privacy_level=PrivacyLevel.L1),
        )
        assert result.ok
        assert requests[0].url.path == "/prod-api/coupons/user-1"
        assert requests[0].headers["Authorization"] == "Bearer test-token"
        runs = await admin.get(f"/api/v1/admin/skills/{skill_id}/runs", headers=headers)
        assert runs.status_code == 200
        assert runs.json()[0]["ok"] is True
        assert "user_id" not in str(runs.json())
        denied_path = await handlers[0].execute(
            handlers[0].arguments_model.model_validate({"user_id": "../admin"}),
            ToolContext(privacy_level=PrivacyLevel.L1),
        )
        assert not denied_path.ok and denied_path.reason_code == "invalid_path_parameter"
        assert len(requests) == 1
        await admin.put(
            "/api/v1/admin/skills/connections/partner-system",
            json={**connection.model_dump(), "enabled": False},
            headers=headers,
        )
        assert not await provider.select("查询情侣卡券", privacy_level=PrivacyLevel.L1)
        revoked = await handlers[0].execute(
            handlers[0].arguments_model.model_validate({"user_id": "second"}),
            ToolContext(privacy_level=PrivacyLevel.L1),
        )
        assert not revoked.ok and revoked.reason_code == "connection_disabled_or_path_denied"
        assert len(requests) == 1
    await http_client.close()


def test_generic_connection_rejects_unsafe_configuration() -> None:
    with pytest.raises(ValueError, match="https"):
        SkillConnection(
            id="partner-system",
            base_url="http://partner.example.test",
            auth_type="none",
            allowed_paths=["/coupons"],
        )
    with pytest.raises(ValueError, match="allowed_path"):
        SkillConnection(
            id="partner-system",
            base_url="https://partner.example.test",
            auth_type="none",
            allowed_paths=["/../admin"],
        )
    with pytest.raises(ValueError, match="https"):
        SkillConnection(
            id="partner-system",
            base_url="https://partner.example.test/../admin",
            auth_type="none",
            allowed_paths=["/coupons"],
        )


@pytest.mark.asyncio
async def test_login_bearer_connection_reauthenticates_without_exposing_password(
    database: Database,
) -> None:
    connections = SkillConnectionStore(database)
    await connections.put(
        SkillConnection(
            id="pnkx-admin",
            base_url="https://pnkx.example.test/prod-api",
            auth_type="login_bearer",
            secret_ref="env:PNKX_PASSWORD",
            username_ref="env:PNKX_USERNAME",
            allowed_auth_paths=["/clientLogin"],
            allowed_paths=["/px/card/getCardByUserId"],
            enabled=True,
        )
    )
    logins = 0
    reads = 0

    def remote(request: httpx.Request) -> httpx.Response:
        nonlocal logins, reads
        if request.url.path == "/prod-api/clientLogin":
            logins += 1
            assert request.method == "POST"
            assert request.content == b'{"userName":"alice","password":"private-password"}'
            return httpx.Response(200, json={"code": 200, "token": f"issued-{logins}"})
        reads += 1
        assert request.url.path == "/prod-api/px/card/getCardByUserId"
        assert request.headers["Authorization"] == f"Bearer issued-{logins}"
        assert b"private-password" not in request.content
        if reads == 1:
            return httpx.Response(401, json={"code": 401})
        return httpx.Response(200, json={"code": 200, "data": [{"title": "晚餐券"}]})

    auth = SkillLoginAuth(type="login_bearer", path="/clientLogin")
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as outbound:
        client = SkillHttpClient(
            connections,
            secrets=EnvSecretProvider(
                {"PNKX_USERNAME": "alice", "PNKX_PASSWORD": "private-password"}
            ),
            client=outbound,
        )
        store = SkillStore(database)
        skill = await store.create(
            SkillDocument.model_validate(
                {
                    "name": "pnkx-lovers-card",
                    "description": "查询情侣卡券",
                    "instructions": "查询我的卡券",
                    "api": {
                        "schema_version": 1,
                        "connection": "pnkx-admin",
                        "auth": auth.model_dump(),
                        "operations": [
                            {
                                "name": "my_cards",
                                "description": "当前用户持有的有效卡券",
                                "method": "GET",
                                "path": "/px/card/getCardByUserId",
                                "risk": "read",
                            }
                        ],
                    },
                }
            ),
            source="created",
        )
        await store.set_enabled(skill.id, True)
        provider = SkillToolProvider(store, connections=connections, http_client=client)
        handlers = await provider.select("查看我的情侣卡券", privacy_level=PrivacyLevel.L1)
        assert len(handlers) == 1
        for _ in range(2):
            result = await handlers[0].execute(
                handlers[0].arguments_model.model_validate({}),
                ToolContext(privacy_level=PrivacyLevel.L1),
            )
            assert result.ok and "晚餐券" in result.data["value"]
        assert (logins, reads) == (2, 3)
        with pytest.raises(Exception, match="connection_auth_path_denied"):
            await client.skill_get(
                "pnkx-admin",
                "/px/card/getCardByUserId",
                "/px/card/getCardByUserId",
                params={},
                auth=SkillLoginAuth(type="login_bearer", path="/login"),
            )
        assert (logins, reads) == (2, 3)


@pytest.mark.asyncio
async def test_skill_scoped_encrypted_credentials_can_be_saved_used_and_revoked(
    database: Database,
) -> None:
    store = SkillStore(database)
    skill = await store.create(
        SkillDocument.model_validate(
            {
                "name": "pnkx-lovers-card",
                "description": "查询情侣卡券",
                "instructions": "查询我的卡券",
                "api": {
                    "schema_version": 1,
                    "connection": "pnkx-admin",
                    "auth": {"type": "login_bearer", "path": "/clientLogin"},
                    "operations": [
                        {
                            "name": "my_cards",
                            "description": "当前用户持有的有效卡券",
                            "method": "GET",
                            "path": "/px/card/getCardByUserId",
                            "risk": "read",
                        }
                    ],
                },
            }
        ),
        source="created",
    )
    await store.set_enabled(skill.id, True)
    connections = SkillConnectionStore(database)
    await connections.put(
        SkillConnection(
            id="pnkx-admin",
            base_url="https://pnkx.example.test/prod-api",
            auth_type="login_bearer",
            allowed_auth_paths=["/clientLogin"],
            allowed_paths=["/px/card/getCardByUserId"],
            enabled=True,
        )
    )
    credentials = SkillCredentialStore(database, key=Fernet.generate_key())
    requests: list[httpx.Request] = []

    def remote(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/clientLogin"):
            assert request.content == b'{"userName":"alice","password":"private-password"}'
            return httpx.Response(200, json={"code": 200, "token": "issued-token"})
        assert request.headers["Authorization"] == "Bearer issued-token"
        return httpx.Response(200, json={"code": 200, "data": [{"title": "晚餐券"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as outbound:
        client = SkillHttpClient(connections, credentials=credentials, client=outbound)
        provider = SkillToolProvider(store, connections=connections, http_client=client)
        app = FastAPI()
        app.include_router(
            create_admin_skills_router(
                store,
                admin_token="admin-secret",
                connections=connections,
                credentials=credentials,
                tool_provider=provider,
            )
        )
        path = f"/api/v1/admin/skills/{skill.id}/credential"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as admin:
            assert (await admin.get(path)).status_code == 401
            headers = {"Authorization": "Bearer admin-secret"}
            status = await admin.get(path, headers=headers)
            assert status.status_code == 200 and not status.json()["configured"]
            assert not await provider.select("查看我的情侣卡券", privacy_level=PrivacyLevel.L1)
            saved = await admin.put(
                path,
                headers=headers,
                json={"username": "alice", "password": "private-password"},
            )
            assert saved.status_code == 200 and saved.json()["configured"]
            assert "alice" not in saved.text and "private-password" not in saved.text
            async with database.sessions() as session:
                record = await session.get(SkillCredentialRecord, skill.id)
                assert record is not None
                assert "alice" not in record.ciphertext
                assert "private-password" not in record.ciphertext
            wrong_key_status = await SkillCredentialStore(
                database, key=Fernet.generate_key()
            ).status(skill.id)
            assert wrong_key_status.configured and not wrong_key_status.key_ready
            handlers = await provider.select("查看我的情侣卡券", privacy_level=PrivacyLevel.L1)
            assert len(handlers) == 1
            result = await handlers[0].execute(
                handlers[0].arguments_model.model_validate({}),
                ToolContext(privacy_level=PrivacyLevel.L1),
            )
            assert result.ok and "晚餐券" in result.data["value"]
            assert len(requests) == 2
            removed = await admin.delete(path, headers=headers)
            assert removed.status_code == 200 and not removed.json()["configured"]
            assert not await provider.select("查看我的情侣卡券", privacy_level=PrivacyLevel.L1)
            revoked = await handlers[0].execute(
                handlers[0].arguments_model.model_validate({}),
                ToolContext(privacy_level=PrivacyLevel.L1),
            )
            assert not revoked.ok and revoked.reason_code == "connection_secret_unavailable"
            assert len(requests) == 2


@pytest.mark.asyncio
async def test_my_cards_request_ranks_current_user_operation(database: Database) -> None:
    skill = await SkillStore(database).create(
        SkillDocument(name="pnkx-lovers-card", description="查询情侣卡券", instructions="查询卡券"),
        source="created",
    )
    list_operation = SkillOperation(
        name="list_cards",
        description="卡券定义分页列表",
        method="GET",
        path="/px/card/list",
        risk="read",
    )
    mine_operation = SkillOperation(
        name="my_cards",
        description="当前用户持有的有效卡券",
        method="GET",
        path="/px/card/getCardByUserId",
        risk="read",
    )
    assert _relevance("查看我的情侣卡券", skill, mine_operation) > _relevance(
        "查看我的情侣卡券", skill, list_operation
    )


@pytest.mark.asyncio
async def test_repeated_failure_creates_one_review_suggestion(database: Database) -> None:
    store = SkillStore(database)
    skill = await store.create(
        SkillDocument(name="test-skill", description="测试技能", instructions="测试说明"),
        source="created",
    )
    for _ in range(3):
        await store.record_run(
            skill_id=skill.id,
            skill_version=1,
            connection_id="partner-system",
            operation="list_coupons",
            ok=False,
            reason_code="connection_http_404",
            latency_ms=5,
        )
    suggestions = await store.suggestions()
    assert len(suggestions) == 1
    assert suggestions[0].kind == "contract"
    assert len(suggestions[0].evidence_run_ids) == 3
    await store.record_run(
        skill_id=skill.id,
        skill_version=1,
        connection_id="partner-system",
        operation="list_coupons",
        ok=False,
        reason_code="connection_http_404",
        latency_ms=5,
    )
    assert len(await store.suggestions()) == 1
    await store.dismiss_suggestion(suggestions[0].id)
    assert await store.suggestions() == []
