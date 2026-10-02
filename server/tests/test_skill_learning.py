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
from app.skills.models import SkillApiManifest, SkillDocument, SkillOperation, SkillParameter
from app.skills.store import SkillDraftView, SkillStore

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
        await learner.harvest(text=USER_TEXT, turn_id=None, runs=[], backend=NeverBackend()) is None
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

    async def fake_verify(draft: SkillDraftView, **kwargs: object) -> SkillDraftView:
        probed.append(draft)
        return draft

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
        await generator.generate_revision(BASE_DOCUMENT, correction=USER_TEXT, run_notes=[])


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


@pytest.mark.asyncio
async def test_attributed_version_is_not_overridden_by_unrelated_latest_run(
    database: Database,
) -> None:
    store = await _seed_skill(database)
    skill = await store.get_by_name("partner-coupons")
    assert skill is not None
    await store.record_run(
        skill_id=skill.id,
        skill_version=9,
        connection_id="partner-system",
        operation="coupon_list",
        ok=False,
        reason_code="upstream_404",
        latency_ms=1,
    )
    backend = QueuedBackend([_decide_response(), _revision_response()])
    learner = SkillRevisionLearner(store, SkillDraftGenerator(backend=backend))
    draft = await learner.harvest(
        text=USER_TEXT,
        turn_id=None,
        runs=[TurnSkillRun(RUN.tool_name, False, "upstream_404", skill_version=1)],
        backend=backend,
    )
    assert draft is not None


@pytest.mark.asyncio
async def test_attributed_old_version_is_rejected(database: Database) -> None:
    store = await _seed_skill(database)
    backend = QueuedBackend([_decide_response()])
    learner = SkillRevisionLearner(store, SkillDraftGenerator(backend=backend))
    assert (
        await learner.harvest(
            text=USER_TEXT,
            turn_id=None,
            runs=[TurnSkillRun(RUN.tool_name, False, "upstream_404", skill_version=9)],
            backend=backend,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before_status", "after_status", "required", "expected", "passed"),
    [
        (404, 200, False, "improved", True),
        (200, 404, False, "regressed", False),
        (200, 200, False, "unchanged", True),
        (404, 404, False, "inconclusive", False),
        (404, 200, True, "inconclusive", False),
    ],
)
async def test_revision_verifies_changed_operation_and_persists_comparison(
    database: Database,
    before_status: int,
    after_status: int,
    required: bool,
    expected: str,
    passed: bool,
) -> None:
    import httpx

    from app.skills.connections import SkillConnection, SkillConnectionStore, SkillHttpClient
    from app.skills.drafts import verify_skill_draft

    store = await _seed_skill(database)
    target = await store.get_by_name("partner-coupons")
    assert target is not None and BASE_DOCUMENT.api is not None
    changed = BASE_DOCUMENT.api.operations[0].model_copy(
        update={
            "path": "/coupons/search",
            "parameters": {"keyword": SkillParameter(type="string", required=True)}
            if required
            else {},
        }
    )
    document = BASE_DOCUMENT.model_copy(
        update={
            "api": BASE_DOCUMENT.api.model_copy(
                update={
                    "operations": [
                        changed,
                        SkillOperation(
                            name="health",
                            description="探活",
                            method="GET",
                            path="/health",
                            risk="read",
                        ),
                    ]
                }
            ),
        }
    )
    assert document.api is not None
    # Keep an unrelated operation in both versions. It must never be probed.
    baseline = BASE_DOCUMENT.model_copy(
        update={
            "api": BASE_DOCUMENT.api.model_copy(
                update={
                    "operations": [
                        BASE_DOCUMENT.api.operations[0],
                        document.api.operations[1],
                    ]
                }
            ),
        }
    )
    target = await store.revise(target.id, baseline)
    draft = await store.save_draft(
        SkillProposal(document=document, warnings=[], evidence=[], executable=False),
        system_name="partner-system",
        source="learning",
        target_skill_id=target.id,
        base_version=target.version,
    )
    assert draft is not None
    connections = SkillConnectionStore(database)
    await connections.put(
        SkillConnection(
            id="partner-system",
            base_url="https://partner.example",
            auth_type="none",
            allowed_paths=["/coupons", "/coupons/search", "/health"],
            enabled=True,
        )
    )
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path != "/health"
        status = before_status if request.url.path == "/coupons" else after_status
        return httpx.Response(status, json={"private_payload": "must-not-persist"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await verify_skill_draft(
            draft,
            store=store,
            connections=connections,
            http_client=SkillHttpClient(connections, client=client),
        )
    assert result.verify_status == ("passed" if passed else "failed")
    assert result.verification_report is not None
    assert result.verification_report["outcome"] == expected
    assert paths == (["/coupons"] if required else ["/coupons", "/coupons/search"])
    persisted = await store.get_draft(draft.id)
    assert persisted is not None and persisted.verification_report == result.verification_report
    assert "must-not-persist" not in persisted.model_dump_json()
    current = await store.get(target.id)
    assert current is not None and current.version == target.version
    await store.dismiss_draft(draft.id)
    reopened = await store.save_draft(
        SkillProposal(document=document, warnings=[], evidence=[], executable=False),
        system_name="partner-system",
        source="learning",
        target_skill_id=target.id,
        base_version=target.version,
    )
    assert reopened is not None and reopened.verification_report is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_revision_does_not_probe_description_only_or_stale_contracts(
    database: Database,
    stale: bool,
) -> None:
    from app.skills.connections import SkillConnectionStore, SkillHttpClient
    from app.skills.drafts import verify_skill_draft

    store = await _seed_skill(database)
    target = await store.get_by_name("partner-coupons")
    assert target is not None
    document = BASE_DOCUMENT.model_copy(update={"instructions": "改为先查询再解释。"})
    draft = await store.save_draft(
        SkillProposal(document=document, warnings=[], evidence=[], executable=False),
        system_name="partner-system",
        source="learning",
        target_skill_id=target.id,
        base_version=target.version,
    )
    assert draft is not None
    if stale:
        await store.revise(target.id, document)
    connections = SkillConnectionStore(database)
    # No connections configured: an unrelated GET would fail differently.
    client = SkillHttpClient(connections)
    try:
        result = await verify_skill_draft(
            draft,
            store=store,
            connections=connections,
            http_client=client,
        )
    finally:
        await client.close()
    assert result.verify_status == "failed"
    assert result.verify_reason == (
        "revision_baseline_stale" if stale else "revision_checks_incomplete"
    )
    assert result.verification_report is not None
    assert result.verification_report["checks"] == []
    assert result.verification_report["outcome"] == "inconclusive"


async def test_l2_learning_never_auto_probes_external_connection(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.schemas import PrivacyLevel

    async def forbidden(*args: object, **kwargs: object) -> SkillDraftView:
        raise AssertionError("L2 source must not trigger external probing")

    monkeypatch.setattr("app.skills.learning.verify_skill_draft", forbidden)
    store = await _seed_skill(database)
    backend = QueuedBackend([_decide_response(), _revision_response()])
    learner = SkillRevisionLearner(store, SkillDraftGenerator(backend=backend))
    draft = await learner.harvest(
        text=USER_TEXT,
        turn_id=None,
        runs=[RUN],
        backend=backend,
        privacy_level=PrivacyLevel.L2,
    )
    assert draft is not None
    persisted = await store.get_draft(draft.id)
    assert persisted is not None
    assert persisted.verify_reason == "local_only_source_probe_skipped"
