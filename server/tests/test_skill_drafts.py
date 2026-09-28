from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.admin_skills import create_admin_skills_router
from app.config.models import WebFetchConfig
from app.db import Base, Database, create_database
from app.llm import CompletionRequest, CompletionResult, LLMRoute
from app.schemas import PrivacyLevel
from app.skills.drafts import SkillDraftAssistant, _ProposeSkillArgs
from app.skills.generator import SkillDraftGenerator
from app.skills.store import SkillStore
from app.tools import webfetch as webfetch_module
from app.tools.contracts import ToolContext
from app.tools.webfetch import FetchWebpageTool


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield value


class FakeBackend:
    def __init__(self, response: dict[str, object], *, finish_reason: str | None = None) -> None:
        self.response = response
        self.finish_reason = finish_reason
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
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
        raise AssertionError("backend must not be called")


def _proposal_response() -> dict[str, object]:
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


def _assistant(store: SkillStore, backend: FakeBackend) -> SkillDraftAssistant:
    return SkillDraftAssistant(store, SkillDraftGenerator(backend=backend))


DOC_TEXT = "帮我把这个接口沉淀成技能：GET /coupons 查询卡券列表"


@pytest.mark.asyncio
async def test_chat_tool_saves_pending_draft_and_dedupes(database: Database) -> None:
    store = SkillStore(database)
    assistant = _assistant(store, FakeBackend(_proposal_response()))
    args = _ProposeSkillArgs.model_validate(
        {"system_name": "partner-system", "source": "GET /coupons", "reason": "用户想沉淀技能"}
    )
    result = await assistant.execute(args, ToolContext(privacy_level=PrivacyLevel.L1))
    assert result.ok
    assert result.data["status"] == "pending"
    assert result.data["skill_name"] == "partner-coupons"
    drafts = await store.drafts()
    assert len(drafts) == 1
    assert drafts[0].source == "chat"
    assert drafts[0].document.api is not None
    # 同样内容再次提案：去重，不产生第二条待审草稿
    duplicate = await assistant.execute(args, ToolContext(privacy_level=PrivacyLevel.L1))
    assert duplicate.ok and duplicate.data["status"] == "duplicate_pending"
    assert len(await store.drafts()) == 1


@pytest.mark.asyncio
async def test_chat_tool_rejects_invalid_input(database: Database) -> None:
    store = SkillStore(database)
    assistant = _assistant(store, FakeBackend(_proposal_response()))
    invalid = await assistant.execute(
        _ProposeSkillArgs.model_validate(
            {"system_name": "Bad Name", "source": "GET /coupons", "reason": "r"}
        ),
        ToolContext(privacy_level=PrivacyLevel.L1),
    )
    assert not invalid.ok and invalid.reason_code == "invalid_system_name"
    rejected = await assistant.execute(
        _ProposeSkillArgs.model_validate(
            {"system_name": "partner-system", "source": "GET /other", "reason": "r"}
        ),
        ToolContext(privacy_level=PrivacyLevel.L1),
    )
    assert not rejected.ok and rejected.reason_code == "generated_path_not_in_document"
    assert await store.drafts() == []


@pytest.mark.asyncio
async def test_harvest_requires_marker_and_verbatim_source(database: Database) -> None:
    store = SkillStore(database)
    extraction = FakeBackend(
        {"propose": True, "system_name": "partner-system", "source": "GET /coupons"}
    )
    assistant = _assistant(store, FakeBackend(_proposal_response()))
    # 无文档特征：正则闸门直接短路，不调用任何模型
    assert (
        await assistant.harvest(text="今天天气怎么样", turn_id=None, backend=NeverBackend()) is None
    )
    # 提取结果非逐字出自用户消息：拒绝，防止补造接口
    assert await assistant.harvest(text=DOC_TEXT, turn_id=None, backend=extraction) is not None
    drafts = await store.drafts()
    assert len(drafts) == 1 and drafts[0].source == "harvest"
    hallucinated = FakeBackend(
        {"propose": True, "system_name": "partner-system", "source": "GET /coupons/hidden"}
    )
    assistant2 = _assistant(store, FakeBackend(_proposal_response()))
    assert (
        await assistant2.harvest(
            text="看看 GET /coupons 的文档", turn_id=None, backend=hallucinated
        )
        is None
    )
    # 闲聊判定：提取器返回 propose=false
    declined = FakeBackend({"propose": False})
    assert await assistant2.harvest(text=DOC_TEXT, turn_id=None, backend=declined) is None
    assert len(await store.drafts()) == 1


@pytest.mark.asyncio
async def test_draft_review_lifecycle_and_api(database: Database) -> None:
    store = SkillStore(database)
    backend = FakeBackend(_proposal_response())
    assistant = _assistant(store, backend)
    draft = await assistant.harvest(
        text=DOC_TEXT,
        turn_id=None,
        backend=FakeBackend(
            {"propose": True, "system_name": "partner-system", "source": "GET /coupons"}
        ),
    )
    assert draft is not None
    app = FastAPI()
    app.include_router(create_admin_skills_router(store, admin_token="secret"))
    headers = {"Authorization": "Bearer secret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as admin:
        assert (await admin.get("/api/v1/admin/skills/drafts")).status_code == 401
        listed = await admin.get("/api/v1/admin/skills/drafts", headers=headers)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [str(draft.id)]
        approved = await admin.post(
            f"/api/v1/admin/skills/drafts/{draft.id}/approve", headers=headers
        )
        assert approved.status_code == 200
        assert approved.json()["source"] == "generated"
        assert approved.json()["enabled"] is False
        assert await admin.get("/api/v1/admin/skills/drafts", headers=headers)
        # 已审阅的草稿不能重复处理
        again = await admin.post(f"/api/v1/admin/skills/drafts/{draft.id}/approve", headers=headers)
        assert again.status_code == 409
        assert (
            await admin.post(f"/api/v1/admin/skills/drafts/{uuid4()}/dismiss", headers=headers)
        ).status_code == 404


@pytest.mark.asyncio
async def test_dismissed_draft_reopens_and_name_conflict_blocks_approval(
    database: Database,
) -> None:
    store = SkillStore(database)
    backend = FakeBackend(_proposal_response())
    assistant = _assistant(store, backend)
    extraction = FakeBackend(
        {"propose": True, "system_name": "partner-system", "source": "GET /coupons"}
    )
    draft = await assistant.harvest(text=DOC_TEXT, turn_id=None, backend=extraction)
    assert draft is not None
    dismissed = await store.dismiss_draft(draft.id)
    assert dismissed.status == "dismissed"
    assert await store.drafts() == []
    # 同内容再次收割：复用原记录并重新进入待审
    reopened = await assistant.harvest(text=DOC_TEXT, turn_id=None, backend=extraction)
    assert reopened is not None and reopened.id == draft.id and reopened.status == "pending"
    # 与现有技能重名时审批失败，草稿保持待审
    from app.skills import SkillDocument

    await store.create(
        SkillDocument(name="partner-coupons", description="占位", instructions="占位说明"),
        source="created",
    )
    with pytest.raises(ValueError, match="skill_name_exists"):
        await store.approve_draft(reopened.id)
    assert [item.status for item in await store.drafts(status=None)] == ["pending"]


def _web_fetch(handler: object, *, enabled: bool = True) -> FetchWebpageTool:
    config = WebFetchConfig(enabled=enabled)
    return FetchWebpageTool(
        lambda: config,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


_DOC_URL = "https://partner.example/api-docs"
_URL_ARGS = {"system_name": "partner-system", "url": _DOC_URL, "reason": "用户给链接想沉淀技能"}


@pytest.mark.asyncio
async def test_chat_tool_fetches_url_and_drafts(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 传输层被 Mock，跳过真实 DNS 校验（SSRF 逻辑在 test_webfetch 单独覆盖）。
    monkeypatch.setattr(webfetch_module, "_assert_public_url", lambda url: None)
    fetched = "GET /coupons 查询卡券列表"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, text=fetched)

    store = SkillStore(database)
    backend = FakeBackend(_proposal_response())
    assistant = SkillDraftAssistant(
        store, SkillDraftGenerator(backend=backend), web_fetch=_web_fetch(handler)
    )
    result = await assistant.execute(
        _ProposeSkillArgs.model_validate(_URL_ARGS),
        ToolContext(privacy_level=PrivacyLevel.L1),
    )
    assert result.ok and result.data["status"] == "pending"
    drafts = await store.drafts()
    assert len(drafts) == 1 and drafts[0].source == "chat"
    # 生成器读到的是服务端抓取的网页正文，而不是模型转述
    assert any(
        fetched in message.content
        for request in backend.requests
        for message in request.messages
        if message.role == "user"
    )


@pytest.mark.asyncio
async def test_chat_tool_url_refusals(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webfetch_module, "_assert_public_url", lambda url: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="GET /coupons")

    store = SkillStore(database)
    backend = FakeBackend(_proposal_response())
    generator = SkillDraftGenerator(backend=backend)
    assistant = SkillDraftAssistant(store, generator, web_fetch=_web_fetch(handler))
    # L2 私密会话不允许服务端外发链接
    l2 = await assistant.execute(
        _ProposeSkillArgs.model_validate(_URL_ARGS),
        ToolContext(privacy_level=PrivacyLevel.L2),
    )
    assert not l2.ok and l2.reason_code == "web_fetch_requires_l0_or_l1"
    # 配置关闭或未注入抓取工具：拒绝且不触发生成器
    disabled = SkillDraftAssistant(store, generator, web_fetch=_web_fetch(handler, enabled=False))
    blocked = await disabled.execute(
        _ProposeSkillArgs.model_validate(_URL_ARGS),
        ToolContext(privacy_level=PrivacyLevel.L1),
    )
    assert not blocked.ok and blocked.reason_code == "web_fetch_disabled"
    nofetch = SkillDraftAssistant(store, generator)
    missing = await nofetch.execute(
        _ProposeSkillArgs.model_validate(_URL_ARGS),
        ToolContext(privacy_level=PrivacyLevel.L1),
    )
    assert not missing.ok and missing.reason_code == "web_fetch_disabled"
    # url 与 source 都缺
    empty = await assistant.execute(
        _ProposeSkillArgs.model_validate({"system_name": "partner-system", "reason": "r"}),
        ToolContext(privacy_level=PrivacyLevel.L1),
    )
    assert not empty.ok and empty.reason_code == "source_required"
    # 抓取失败原因透传给模型
    broken = SkillDraftAssistant(
        store, generator, web_fetch=_web_fetch(lambda request: httpx.Response(404))
    )
    failed = await broken.execute(
        _ProposeSkillArgs.model_validate(_URL_ARGS),
        ToolContext(privacy_level=PrivacyLevel.L1),
    )
    assert not failed.ok and failed.reason_code == "http_error"
    assert await store.drafts() == []
    assert backend.requests == []
