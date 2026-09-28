from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.admin_skills import create_admin_skills_router
from app.db import Base, create_database
from app.llm import CompletionRequest, CompletionResult, LLMRoute
from app.schemas import PrivacyLevel
from app.skills.generator import SkillDraftGenerator, extract_document
from app.skills.markdown_api import compile_markdown_api
from app.skills.store import SkillStore


class FakeBackend:
    def __init__(self, response: dict[str, object], *, finish_reason: str | None = None) -> None:
        self.response = response
        self.finish_reason = finish_reason
        self.request: CompletionRequest | None = None

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.request = request
        return CompletionResult(
            text=json.dumps(self.response),
            provider="openai_compatible",
            model="fake",
            endpoint="fake",
            route=LLMRoute.PRIVATE,
            latency_ms=1,
            finish_reason=self.finish_reason,
        )


class NeverBackend:
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        del request
        raise AssertionError("explicit endpoint table should not call the model")


def _response() -> dict[str, object]:
    return {
        "document": {
            "name": "partner-coupons",
            "description": "查询情侣卡券",
            "instructions": "用户询问卡券时，先读取列表。",
            "api": {
                "schema_version": 1,
                "connection": "partner-system",
                "operations": [
                    {
                        "name": "coupon_list",
                        "description": "查询情侣卡券",
                        "method": "GET",
                        "path": "/coupons",
                        "risk": "read",
                        "parameters": {},
                    }
                ],
            },
        },
        "warnings": [],
        "evidence": ["GET /coupons"],
    }


@pytest.mark.asyncio
async def test_generate_review_save_and_secret_redaction() -> None:
    database = create_database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    backend = FakeBackend(_response())
    app = FastAPI()
    app.include_router(
        create_admin_skills_router(
            SkillStore(database),
            admin_token="secret",
            generator=SkillDraftGenerator(backend=backend),
        )
    )
    headers = {"Authorization": "Bearer secret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.post(
                "/api/v1/admin/skills/generate",
                json={"system_name": "partner-system", "source": "GET /coupons"},
            )
        ).status_code == 401
        response = await client.post(
            "/api/v1/admin/skills/generate",
            json={
                "system_name": "partner-system",
                "source": "GET /coupons\nAuthorization: Bearer real-secret",
            },
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["executable"] is False
        assert response.json()["warnings"]
        assert backend.request is not None
        assert backend.request.privacy_level == PrivacyLevel.L2
        assert backend.request.route == LLMRoute.PRIVATE
        assert "real-secret" not in backend.request.messages[1].content
        assert (await client.get("/api/v1/admin/skills", headers=headers)).json() == []
        uploaded = await client.post(
            "/api/v1/admin/skills/generate-upload?system_name=partner-system",
            files={"file": ("api.md", b"GET /coupons", "text/markdown")},
            headers=headers,
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["document"]["name"] == "partner-coupons"
        saved = await client.post(
            "/api/v1/admin/skills/generated",
            json=response.json()["document"],
            headers=headers,
        )
        assert saved.status_code == 201
        assert saved.json()["source"] == "generated"
        assert saved.json()["enabled"] is False
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_generated_path_must_be_in_document() -> None:
    backend = FakeBackend(_response())
    with pytest.raises(ValueError, match="generated_path_not_in_document"):
        await SkillDraftGenerator(backend=backend).generate(
            "GET /other", system_name="partner-system"
        )


@pytest.mark.asyncio
async def test_truncated_model_output_is_reported_explicitly() -> None:
    backend = FakeBackend(_response(), finish_reason="length")
    with pytest.raises(ValueError, match="generated_skill_truncated"):
        await SkillDraftGenerator(backend=backend).generate(
            "An unstructured usage guide without endpoint tables", system_name="partner-system"
        )


def test_document_extraction() -> None:
    assert extract_document("api.html", b"<h1>GET /coupons</h1>") == "GET /coupons"
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>GET /coupons</w:t></w:r></w:p></w:body></w:document>",
        )
    assert extract_document("api.docx", data.getvalue()) == "GET /coupons"
    with pytest.raises(ValueError, match="unsupported_document_format"):
        extract_document("api.pdf", b"contents")


LOVERS_CARD_DOC = """# 情侣卡券 后端 API 文档

| # | 方法 | 路径 | 说明 |
|---|---|---|---|
| 1 | GET | `/px/card/list` | 卡券定义分页列表 |
| 2 | GET | `/px/card/export` | 导出卡券列表 Excel |
| 3 | POST | `/px/card/useCard` | 发起使用 |

认证：`Authorization: Bearer <token>`。

### 3.1 GET /px/card/list — 卡券列表
Query：`title`、`pageNum`、`pageSize`。

### 3.2 GET /px/card/export
生成 Excel 文件。

### 3.3 POST /px/card/useCard — 使用卡券
Body：`cardId`（必填）、`instructions`。
"""


@pytest.mark.asyncio
async def test_markdown_api_upload_compiles_without_model() -> None:
    database = create_database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = FastAPI()
    app.include_router(
        create_admin_skills_router(
            SkillStore(database),
            admin_token="secret",
            generator=SkillDraftGenerator(backend=NeverBackend()),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/admin/skills/generate-upload?system_name=pnkx",
            files={"file": ("API_LOVERS_CARD.md", LOVERS_CARD_DOC.encode(), "text/markdown")},
            headers={"Authorization": "Bearer secret"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["document"]["name"] == "pnkx-lovers-card"
        assert body["document"]["api"]["connection"] == "pnkx-admin"
        operations = body["document"]["api"]["operations"]
        assert [item["method"] for item in operations] == ["GET", "POST"]
        assert operations[0]["parameters"]["pageNum"]["location"] == "query"
        assert operations[1]["parameters"]["cardId"]["required"] is True
        assert any("Bearer" in warning for warning in body["warnings"])
    await database.engine.dispose()


def test_markdown_login_contract_becomes_skill_auth() -> None:
    from app.skills.markdown_api import compile_markdown_api

    source = """# PNKX API
| # | 方法 | 路径 | 说明 |
|---|---|---|---|
| 1 | POST | `/clientLogin` | 用户名密码登录 |
| 2 | GET | `/px/card/getCardByUserId` | 我的卡券 |

### 1.1 POST /clientLogin — 登录
Body：`userName`、`password`。
Response：`token` 为 Bearer 令牌。

### 1.2 GET /px/card/getCardByUserId — 我的卡券
认证：`Authorization: Bearer <token>`。
"""
    compiled = compile_markdown_api(source, system_name="pnkx", filename="card.md")
    assert compiled is not None
    document, warnings, _ = compiled
    assert document.api is not None and document.api.auth is not None
    assert document.api.auth.path == "/clientLogin"
    assert [item.path for item in document.api.operations] == ["/px/card/getCardByUserId"]
    assert any("登录契约" in item for item in warnings)


@pytest.mark.asyncio
async def test_missing_skill_migration_returns_actionable_error() -> None:
    database = create_database("sqlite+aiosqlite:///:memory:")
    app = FastAPI()
    app.include_router(create_admin_skills_router(SkillStore(database), admin_token="secret"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/admin/skills/generated",
            json={
                "name": "test-skill",
                "description": "测试",
                "instructions": "操作说明",
                "api": None,
            },
            headers={"Authorization": "Bearer secret"},
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "skill_schema_not_migrated"
    await database.engine.dispose()


def test_markdown_api_headings_without_table() -> None:
    result = compile_markdown_api(
        "# 订单接口\n### 1.1 GET /orders/{orderId} — 查询订单\n路径 orderId。\n",
        system_name="orders",
    )
    assert result is not None
    document, _, _ = result
    assert document.api is not None
    operation = document.api.operations[0]
    assert operation.path == "/orders/{orderId}"
    assert operation.parameters["orderId"].location == "path"
