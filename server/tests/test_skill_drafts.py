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
from app.skills import SkillApiManifest, SkillDocument, SkillOperation
from app.skills.connections import SkillConnection, SkillConnectionStore, SkillHttpClient
from app.skills.drafts import SkillDraftAssistant, _ProposeSkillArgs, verify_skill_draft
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


def _target_document() -> SkillDocument:
    return SkillDocument(
        name="partner-coupons",
        description="查询情侣卡券",
        instructions="用户询问卡券时，先读取列表。",
        api=SkillApiManifest(
            schema_version=1,
            connection="partner-system",
            operations=[
                SkillOperation(
                    name="coupon_list",
                    description="查询情侣卡券",
                    method="GET",
                    path="/coupons",
                    risk="read",
                )
            ],
        ),
    )


_REVISION_ARGS = {
    "system_name": "partner-system",
    "target_skill": "partner-coupons",
    "source": "GET /coupons",
    "reason": "用户纠正了接口用法",
}


@pytest.mark.asyncio
async def test_propose_skill_revision_flow(database: Database) -> None:
    store = SkillStore(database)
    target = await store.create(_target_document(), source="created")
    assert target.version == 1
    revised = _proposal_response()
    revised_document = revised["document"]
    assert isinstance(revised_document, dict)
    revised_document["description"] = "查询情侣卡券（修订）"
    assistant = _assistant(store, FakeBackend(revised))
    result = await assistant.execute(
        _ProposeSkillArgs.model_validate(_REVISION_ARGS),
        ToolContext(privacy_level=PrivacyLevel.L1),
    )
    assert result.ok
    assert result.data["revision_of"] == "partner-coupons"
    assert result.data["base_version"] == 1
    drafts = await store.drafts()
    assert len(drafts) == 1
    assert drafts[0].target_skill_id == target.id
    assert drafts[0].base_version == 1
    # 审批修订：为现有技能创建新版本，名称沿用、内容以草稿为准
    updated = await store.approve_draft(drafts[0].id)
    assert updated.id == target.id
    assert updated.version == 2
    assert updated.name == "partner-coupons"
    assert updated.description == "查询情侣卡券（修订）"
    assert [item.version for item in await store.versions(target.id)] == [2, 1]
    rolled = await store.rollback(target.id, 1)
    assert rolled.version == 3 and rolled.description == "查询情侣卡券"
    reviewed = await store.get_draft(drafts[0].id)
    assert reviewed is not None and reviewed.status == "approved"
    assert reviewed.skill_id == target.id


@pytest.mark.asyncio
async def test_revision_guards(database: Database) -> None:
    store = SkillStore(database)
    target = await store.create(_target_document(), source="created")
    assistant = _assistant(store, FakeBackend(_proposal_response()))
    missing = await assistant.execute(
        _ProposeSkillArgs.model_validate({**_REVISION_ARGS, "target_skill": "ghost"}),
        ToolContext(privacy_level=PrivacyLevel.L1),
    )
    assert not missing.ok and missing.reason_code == "target_skill_not_found"
    mismatch = await assistant.execute(
        _ProposeSkillArgs.model_validate({**_REVISION_ARGS, "system_name": "other-system"}),
        ToolContext(privacy_level=PrivacyLevel.L1),
    )
    # 修订不能悄悄切换连接：文档系统必须与现有技能一致
    assert not mismatch.ok and mismatch.reason_code == "revision_connection_mismatch"
    assert await store.drafts() == []
    accepted = await assistant.execute(
        _ProposeSkillArgs.model_validate(_REVISION_ARGS),
        ToolContext(privacy_level=PrivacyLevel.L1),
    )
    assert accepted.ok
    draft = (await store.drafts())[0]
    # 启用中的技能不允许编辑：审批被拒，草稿保持待审
    await store.set_enabled(target.id, True)
    with pytest.raises(ValueError, match="disable_skill_before_edit"):
        await store.approve_draft(draft.id)
    await store.set_enabled(target.id, False)
    # 起草基线过期：期间技能被改动，审批被拒
    await store.set_api(
        target.id,
        _target_document().api
        or SkillApiManifest(
            schema_version=1,
            connection="partner-system",
            operations=[
                SkillOperation(
                    name="coupon_list",
                    description="查询情侣卡券",
                    method="GET",
                    path="/coupons",
                    risk="read",
                )
            ],
        ),
    )
    with pytest.raises(ValueError, match="skill_version_changed"):
        await store.approve_draft(draft.id)
    assert (await store.drafts())[0].status == "pending"


def _connection_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/coupons"
    return httpx.Response(200, json={"code": 200, "data": []})


@pytest.mark.asyncio
async def test_draft_verification_lifecycle(database: Database) -> None:
    store = SkillStore(database)
    connections = SkillConnectionStore(database)
    await connections.put(
        SkillConnection(
            id="partner-system",
            base_url="https://partner.example",
            auth_type="none",
            allowed_paths=["/coupons"],
            enabled=True,
        )
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(_connection_handler))
    http = SkillHttpClient(connections, client=client)
    try:
        assistant = _assistant(store, FakeBackend(_proposal_response()))
        result = await assistant.execute(
            _ProposeSkillArgs.model_validate(
                {"system_name": "partner-system", "source": "GET /coupons", "reason": "r"}
            ),
            ToolContext(privacy_level=PrivacyLevel.L1),
        )
        assert result.ok
        draft = (await store.drafts())[0]
        # 试跑走真实连接闸门：通过后记录状态与时间
        verified = await verify_skill_draft(
            draft, store=store, connections=connections, http_client=http
        )
        assert verified.verify_status == "passed"
        assert verified.verify_reason is None and verified.verified_at is not None
        # 连接停用后：同内容草稿重新进入待审，试跑给出明确原因
        await store.dismiss_draft(draft.id)
        await connections.put(
            SkillConnection(
                id="partner-system",
                base_url="https://partner.example",
                auth_type="none",
                allowed_paths=["/coupons"],
                enabled=False,
            )
        )
        reopened = await assistant.execute(
            _ProposeSkillArgs.model_validate(
                {"system_name": "partner-system", "source": "GET /coupons", "reason": "r"}
            ),
            ToolContext(privacy_level=PrivacyLevel.L1),
        )
        assert reopened.ok
        failed = await verify_skill_draft(
            (await store.drafts())[0],
            store=store,
            connections=connections,
            http_client=http,
        )
        assert failed.verify_status == "failed"
        assert failed.verify_reason == "connection_disabled"
        # 说明型草稿（无 api 契约）无法试跑，原因可诊断
        no_api = _proposal_response()
        no_api_document = no_api["document"]
        assert isinstance(no_api_document, dict)
        no_api_document["api"] = None
        plain = SkillDraftAssistant(store, SkillDraftGenerator(backend=FakeBackend(no_api)))
        plain_result = await plain.execute(
            _ProposeSkillArgs.model_validate(
                {"system_name": "partner-system", "source": "GET /coupons", "reason": "r"}
            ),
            ToolContext(privacy_level=PrivacyLevel.L1),
        )
        assert plain_result.ok
        outcome = await verify_skill_draft(
            (await store.drafts())[0],
            store=store,
            connections=connections,
            http_client=http,
        )
        assert outcome.verify_status == "failed"
        assert outcome.verify_reason == "draft_has_no_api"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_draft_verify_endpoint(database: Database) -> None:
    store = SkillStore(database)
    connections = SkillConnectionStore(database)
    await connections.put(
        SkillConnection(
            id="partner-system",
            base_url="https://partner.example",
            auth_type="none",
            allowed_paths=["/coupons"],
            enabled=True,
        )
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(_connection_handler))
    http = SkillHttpClient(connections, client=client)
    try:
        assistant = _assistant(store, FakeBackend(_proposal_response()))
        assert (
            await assistant.execute(
                _ProposeSkillArgs.model_validate(
                    {"system_name": "partner-system", "source": "GET /coupons", "reason": "r"}
                ),
                ToolContext(privacy_level=PrivacyLevel.L1),
            )
        ).ok
        draft = (await store.drafts())[0]
        app = FastAPI()
        app.include_router(
            create_admin_skills_router(
                store,
                admin_token="secret",
                connections=connections,
                http_client=http,
            )
        )
        headers = {"Authorization": "Bearer secret"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as admin:
            assert (
                await admin.post(f"/api/v1/admin/skills/drafts/{uuid4()}/verify", headers=headers)
            ).status_code == 404
            verified = await admin.post(
                f"/api/v1/admin/skills/drafts/{draft.id}/verify", headers=headers
            )
            assert verified.status_code == 200
            assert verified.json()["verify_status"] == "passed"
            # 已审阅的草稿不能再试跑
            approved = await admin.post(
                f"/api/v1/admin/skills/drafts/{draft.id}/approve", headers=headers
            )
            assert approved.status_code == 200
            assert (
                await admin.post(f"/api/v1/admin/skills/drafts/{draft.id}/verify", headers=headers)
            ).status_code == 409
    finally:
        await client.aclose()
