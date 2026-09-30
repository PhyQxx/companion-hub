"""S4 学习闭环（docs/08）：纠正 → 修订候选 → 自动试跑 → Admin 审阅。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from app.db import Base, Database, create_database
from app.llm import CompletionRequest, CompletionResult, LLMRoute
from app.skills.generator import SkillDraftGenerator, SkillProposal
from app.skills.learning import SkillRevisionLearner, TurnSkillRun
from app.skills.models import SkillApiManifest, SkillDocument, SkillOperation
from app.skills.store import SkillStore

USER_TEXT = "查出来的卡券不对，过期日期字段变了，改成 GET /coupons/search 关键词搜"

BASE_DOCUMENT = SkillDocument(
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
                parameters={},
            )
        ],
    ),
)

RUN = TurnSkillRun(
    tool_name="skill.partner-coupons.coupon_list",
    ok=False,
    reason_code="upstream_404",
)


def _revision_response(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "name": "whatever-model-says",
        "description": "查询情侣卡券（支持关键词搜索）",
        "instructions": "用户询问卡券时，可按关键词搜索或读取列表。",
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
                },
                {
                    "name": "coupon_search",
                    "description": "按关键词搜索卡券",
                    "method": "GET",
                    "path": "/coupons/search",
                    "risk": "read",
                    "parameters": {
                        "keyword": {"type": "string", "required": True, "location": "query"}
                    },
                },
            ],
        },
    }
    document.update(overrides)
    return {"document": document, "warnings": [], "evidence": ["GET /coupons/search"]}


class QueuedBackend:
    """按调用次序返回预设响应；记录请求供断言。"""

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = responses
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        payload = self._responses.pop(0)
        return CompletionResult(
            text=json.dumps(payload),
            provider="openai_compatible",
            model="fake",
            endpoint="fake",
            route=LLMRoute.PRIVATE,
            latency_ms=1,
            finish_reason="stop",
        )


class NeverBackend:
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        del request
        raise AssertionError("backend must not be called")


def _decide_response(skill: str = "partner-coupons") -> dict[str, object]:
    return {"revise": True, "skill": skill, "correction": "查出来的卡券不对"}


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield value


async def _seed_skill(database: Database) -> SkillStore:
    store = SkillStore(database)
    await store.create(BASE_DOCUMENT, source="created")
    return store


@pytest.mark.asyncio
async def test_harvest_generates_revision_draft_and_dedupes(database: Database) -> None:
    store = await _seed_skill(database)
    backend = QueuedBackend([_decide_response(), _revision_response(), _decide_response()])
    learner = SkillRevisionLearner(store, SkillDraftGenerator(backend=backend))

    draft = await learner.harvest(text=USER_TEXT, turn_id=uuid4(), runs=[RUN], backend=backend)
    assert draft is not None
    assert draft.source == "learning"
    assert draft.target_skill_id is not None
    assert draft.base_version == 1
    # 修订保持技能身份：名称强制沿用基线，连接不得切换
    assert draft.document.name == "partner-coupons"
    assert draft.document.api is not None
    assert draft.document.api.connection == "partner-system"
    assert any(op.name == "coupon_search" for op in draft.document.api.operations)
    # 同技能已有待审修订：不重复提案
    second = await learner.harvest(text=USER_TEXT, turn_id=uuid4(), runs=[RUN], backend=backend)
    assert second is None
    assert len(await store.drafts()) == 1


@pytest.mark.asyncio
async def test_harvest_ignores_noise_without_llm_call(database: Database) -> None:
    store = await _seed_skill(database)
    learner = SkillRevisionLearner(store, SkillDraftGenerator(backend=NeverBackend()))
    # 没有技能执行证据：不收割
    assert (
        await learner.harvest(text=USER_TEXT, turn_id=None, runs=[], backend=NeverBackend())
        is None
    )
    # 有执行但消息无纠正信号：不调用模型
    calm = "好的，谢谢，卡券列表很清楚。"
    assert (
        await learner.harvest(
            text=calm,
            turn_id=None,
            runs=[TurnSkillRun(tool_name=RUN.tool_name, ok=True, reason_code=None)],
            backend=NeverBackend(),
        )
        is None
    )


@pytest.mark.asyncio
async def test_harvest_rejects_paraphrase_and_unknown_skill(database: Database) -> None:
    store = await _seed_skill(database)
    # 模型改写纠正原文（非逐字）：拒绝
    backend = QueuedBackend(
        [{"revise": True, "skill": "partner-coupons", "correction": "卡券的过期日期改了"}]
    )
    learner = SkillRevisionLearner(store, SkillDraftGenerator(backend=backend))
    assert await learner.harvest(text=USER_TEXT, turn_id=None, runs=[RUN], backend=backend) is None
    # 模型指认列表之外的技能：拒绝
    backend = QueuedBackend([_decide_response(skill="other-skill")])
    learner = SkillRevisionLearner(store, SkillDraftGenerator(backend=backend))
    assert await learner.harvest(text=USER_TEXT, turn_id=None, runs=[RUN], backend=backend) is None
    assert await store.drafts() == []


@pytest.mark.asyncio
async def test_harvest_skips_stale_baseline(database: Database) -> None:
    store = await _seed_skill(database)
    skill = await store.get_by_name("partner-coupons")
    assert skill is not None
    # 最近一次运行来自旧版本 v9（当前 v1）：纠正针对旧契约，不起草
    await store.record_run(
        skill_id=skill.id,
        skill_version=9,
        connection_id="partner-system",
        operation="coupon_list",
        ok=False,
        reason_code="upstream_404",
        latency_ms=1,
    )
    backend = QueuedBackend([_decide_response()])
    learner = SkillRevisionLearner(store, SkillDraftGenerator(backend=backend))
    assert await learner.harvest(text=USER_TEXT, turn_id=None, runs=[RUN], backend=backend) is None
    assert await store.drafts() == []


@pytest.mark.asyncio
async def test_harvest_auto_verifies_draft(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await _seed_skill(database)
    probed: list[object] = []

    async def fake_verify(draft: object, **kwargs: object) -> None:
        probed.append(draft)

    monkeypatch.setattr("app.skills.learning.verify_skill_draft", fake_verify)
    backend = QueuedBackend([_decide_response(), _revision_response()])
    learner = SkillRevisionLearner(
        store,
        SkillDraftGenerator(backend=backend),
        connections=object(),  # type: ignore[arg-type]
        http_client=object(),  # type: ignore[arg-type]
    )
    draft = await learner.harvest(text=USER_TEXT, turn_id=None, runs=[RUN], backend=backend)
    assert draft is not None
    assert len(probed) == 1


@pytest.mark.asyncio
async def test_generate_revision_rejects_connection_switch(database: Database) -> None:
    backend = QueuedBackend(
        [
            _revision_response(
                api={
                    "schema_version": 1,
                    "connection": "other-system",
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
                }
            )
        ]
    )
    generator = SkillDraftGenerator(backend=backend)
    with pytest.raises(ValueError, match="revision_connection_mismatch"):
        await generator.generate_revision(
            BASE_DOCUMENT, correction=USER_TEXT, run_notes=[]
        )


@pytest.mark.asyncio
async def test_generate_revision_rejects_hallucinated_path(database: Database) -> None:
    backend = QueuedBackend(
        [
            _revision_response(
                api={
                    "schema_version": 1,
                    "connection": "partner-system",
                    "operations": [
                        {
                            "name": "coupon_report",
                            "description": "导出报表",
                            "method": "GET",
                            "path": "/coupons/report",
                            "risk": "read",
                            "parameters": {},
                        }
                    ],
                }
            )
        ]
    )
    generator = SkillDraftGenerator(backend=backend)
    with pytest.raises(ValueError, match="revision_path_not_evidenced"):
        await generator.generate_revision(BASE_DOCUMENT, correction=USER_TEXT, run_notes=[])


@pytest.mark.asyncio
async def test_generate_revision_normalizes_write_risk(database: Database) -> None:
    # 模型把 POST 操作标成 read：模型校验器拒绝 read⇒GET 冲突 → 拒绝修订
    backend = QueuedBackend(
        [
            _revision_response(
                api={
                    "schema_version": 1,
                    "connection": "partner-system",
                    "operations": [
                        {
                            "name": "coupon_claim",
                            "description": "领取卡券",
                            "method": "POST",
                            "path": "/coupons/claim",
                            "risk": "read",
                            "parameters": {},
                        }
                    ],
                }
            )
        ]
    )
    generator = SkillDraftGenerator(backend=backend)
    with pytest.raises(ValueError):
        await generator.generate_revision(
            BASE_DOCUMENT,
            correction="改成 POST /coupons/claim 领取卡券",
            run_notes=[],
        )


@pytest.mark.asyncio
async def test_pending_revision_exists(database: Database) -> None:
    store = await _seed_skill(database)
    skill = await store.get_by_name("partner-coupons")
    assert skill is not None
    assert not await store.pending_revision_exists(skill.id)
    proposal = SkillProposal(document=BASE_DOCUMENT, warnings=[], evidence=[], executable=False)
    await store.save_draft(
        proposal,
        system_name="partner-coupons",
        source="learning",
        target_skill_id=skill.id,
        base_version=1,
    )
    assert await store.pending_revision_exists(skill.id)
