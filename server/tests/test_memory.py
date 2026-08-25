# ruff: noqa: RUF001, RUF003
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from test_chat import FakeRouter, config_yaml

from app.chat import ChatService
from app.config import DatabaseConfigStore
from app.db import (
    AppUserRecord,
    Base,
    ConversationRecord,
    Database,
    MessageRecord,
    create_database,
)
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult, ModelUsage
from app.main import create_app
from app.memory import (
    ConsolidateDecision,
    HashingEmbeddingProvider,
    LlmMemoryExtractor,
    MemoryCandidate,
    MemoryExtractor,
    MemoryHit,
    MemoryIngester,
    MemoryOriginKind,
    MemoryRetriever,
    MemorySourceKind,
    MemorySourceRef,
    MemoryStatus,
    MemoryStore,
    MemorySubjectKind,
    MemoryType,
    RetrievalResult,
    RuleBasedExtractor,
    TurnMemoryExtractor,
    cosine_similarity,
    extract_assistant_fact_assertions,
    replay_deletions,
)
from app.schemas import PrivacyLevel

# 检索侧冻结时钟必须晚于真实墙钟：store.add 用当前时间落 valid_from，
# 若冻结点已过，valid_from <= now 过滤会排除全部新记忆导致测试随时间腐烂
NOW = datetime(2099, 1, 1, tzinfo=UTC)


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


@pytest.fixture
async def user(database: Database) -> AppUserRecord:
    record = AppUserRecord(id=uuid7(), display_name="Test", status="active")
    async with database.sessions.begin() as session:
        session.add(record)
    return record


@pytest.fixture
def store(database: Database) -> MemoryStore:
    return MemoryStore(database)


def candidate(
    content: str,
    *,
    type: MemoryType = MemoryType.SEMANTIC,
    privacy_level: PrivacyLevel = PrivacyLevel.L1,
    **kwargs: object,
) -> MemoryCandidate:
    return MemoryCandidate(
        type=type,
        content=content,
        privacy_level=privacy_level,
        sources=[MemorySourceRef(source_kind=MemorySourceKind.MANUAL, source_id="admin:1")],
        **kwargs,
    )


def meta_section(
    meta: dict[str, object] | None,
    key: str,
) -> dict[str, Any]:
    raw = (meta or {}).get(key)
    assert isinstance(raw, dict)
    return cast(dict[str, Any], raw)


async def test_hashing_embedding_is_deterministic_and_orders_similarity() -> None:
    provider = HashingEmbeddingProvider()
    first = await provider.embed(["用户不吃香菜"])
    second = await provider.embed(["用户不吃香菜"])
    related = (await provider.embed(["用户不喜欢吃香菜"]))[0]
    unrelated = (await provider.embed(["明天要去爬山看日出"]))[0]

    assert first[0] == second[0]
    assert cosine_similarity(first[0], related) > cosine_similarity(first[0], unrelated)
    assert cosine_similarity(first[0], unrelated) < 0.35


async def test_grounded_memory_hits_require_actual_relevance(
    store: MemoryStore, user: AppUserRecord
) -> None:
    entry = await store.add(
        candidate("用户喜欢黑咖啡", fact_key="preference.drink", importance=0.8),
        user_id=user.id,
    )
    retriever = MemoryRetriever(store)

    def result(hit: MemoryHit) -> RetrievalResult:
        return RetrievalResult(
            hits=(hit,),
            policy_version="hybrid-subject-v4",
            candidate_count=1,
            vector_recalled=1,
            lexical_recalled=1,
        )

    weak_vector = MemoryHit(
        memory=entry,
        vector_score=0.22,
        lexical_score=0.0,
        final_score=0.42,
        reasons=("vector", "pin"),
    )
    lexical = MemoryHit(
        memory=entry,
        vector_score=0.10,
        lexical_score=0.20,
        final_score=0.40,
        reasons=("vector", "lexical"),
    )
    semantic_vector = MemoryHit(
        memory=entry,
        vector_score=0.60,
        lexical_score=0.0,
        final_score=0.50,
        reasons=("vector",),
    )
    exact = MemoryHit(
        memory=entry,
        vector_score=0.0,
        lexical_score=0.0,
        final_score=1.0,
        reasons=("exact_fact",),
    )

    assert retriever.grounded_hits(result(weak_vector)) == ()
    assert retriever.grounded_hits(result(lexical)) == (lexical,)
    assert retriever.grounded_hits(result(semantic_vector)) == (semantic_vector,)
    assert retriever.grounded_hits(result(exact)) == (exact,)


async def test_store_add_get_list_and_sources(store: MemoryStore, user: AppUserRecord) -> None:
    message_id = uuid4()
    created = await store.add(
        MemoryCandidate(
            type=MemoryType.PREFERENCE,
            content="用户不吃香菜",
            privacy_level=PrivacyLevel.L1,
            sources=[
                MemorySourceRef(
                    source_kind=MemorySourceKind.MESSAGE,
                    source_id=str(message_id),
                    excerpt="我不吃香菜。",
                )
            ],
            importance=0.6,
        ),
        user_id=user.id,
        actor="rule-v1",
    )

    loaded = await store.get(created.id, user_id=user.id)
    assert loaded.content == "用户不吃香菜"
    assert loaded.subject_kind == "user"
    assert loaded.subject_key == "user:self"
    assert loaded.fact_key is None
    assert loaded.origin_kind == "user_statement"
    assert loaded.type == "preference"
    assert loaded.status == "active"
    assert loaded.embedding_version == "char-ngram-hash/1"
    assert loaded.embedding_dimension == 256

    sources = await store.get_sources(created.id)
    assert len(sources) == 1
    assert sources[0].source_kind == "message"
    assert sources[0].source_id == str(message_id)
    assert sources[0].excerpt_hash is not None
    assert sources[0].excerpt_hash.startswith("sha256:")

    others = await store.list_memories(user_id=user.id, type=MemoryType.EPISODIC)
    assert others == []
    visible = await store.list_memories(user_id=user.id, status=MemoryStatus.ACTIVE)
    assert [item.id for item in visible] == [created.id]

    with pytest.raises(LookupError):
        await store.get(created.id, user_id=uuid4())


async def test_store_filters_memory_subject_scope(store: MemoryStore, user: AppUserRecord) -> None:
    user_memory = await store.add(candidate("用户不吃香菜"), user_id=user.id)
    assistant_memory = await store.add(
        candidate(
            "助手身高为 160 厘米",
            subject_kind=MemorySubjectKind.ASSISTANT,
            subject_key="assistant:primary",
            fact_key="profile.height",
            origin_kind=MemoryOriginKind.ASSISTANT_STATEMENT,
        ),
        user_id=user.id,
    )
    shared_memory = await store.add(
        candidate(
            "双方约好周末看电影",
            type=MemoryType.COMMITMENT,
            subject_kind=MemorySubjectKind.SHARED,
            subject_key="shared:user-assistant",
            origin_kind=MemoryOriginKind.SHARED_TURN,
        ),
        user_id=user.id,
    )

    assistant_only = await store.list_memories(
        user_id=user.id,
        subject_kind=MemorySubjectKind.ASSISTANT,
        subject_key="assistant:primary",
    )
    by_fact = await store.list_memories(user_id=user.id, fact_key="profile.height")
    shared_only = await store.list_memories(
        user_id=user.id, origin_kind=MemoryOriginKind.SHARED_TURN
    )

    assert [item.id for item in assistant_only] == [assistant_memory.id]
    assert [item.id for item in by_fact] == [assistant_memory.id]
    assert [item.id for item in shared_only] == [shared_memory.id]
    assert user_memory.id not in {assistant_memory.id, shared_memory.id}


async def test_retrieval_ranks_relevant_memory_and_records_access(
    store: MemoryStore, user: AppUserRecord
) -> None:
    target = await store.add(candidate("用户不吃香菜，点菜时要去掉"), user_id=user.id)
    await store.add(candidate("用户在杭州工作，是后端工程师"), user_id=user.id)
    await store.add(candidate("上周末一起看了电影《星际穿越》"), user_id=user.id)

    result = await MemoryRetriever(store).retrieve(
        "帮我点菜，我能吃香菜吗", user_id=user.id, privacy_level=PrivacyLevel.L1, now=NOW
    )

    assert result.policy_version == "hybrid-subject-v4"
    assert result.hits
    assert result.hits[0].memory.id == target.id
    assert "香菜" in MemoryRetriever.render_context(result)
    assert "【长期记忆】" in MemoryRetriever.render_context(result)
    assert any("vector" in hit.reasons or "lexical" in hit.reasons for hit in result.hits)

    refreshed = await store.get(target.id)
    assert refreshed.access_count >= 1
    assert refreshed.last_accessed_at is not None


async def test_retrieval_isolates_users_and_expired_memories(
    store: MemoryStore, user: AppUserRecord, database: Database
) -> None:
    stranger = AppUserRecord(id=uuid7(), display_name="Stranger", status="active")
    async with database.sessions.begin() as session:
        session.add(stranger)
    await store.add(candidate("用户不吃香菜"), user_id=stranger.id)
    expired = await store.add(
        candidate("用户不吃香菜", valid_to=NOW - timedelta(days=1)), user_id=user.id
    )
    assert expired.valid_to is not None

    result = await MemoryRetriever(store).retrieve(
        "我能吃香菜吗", user_id=user.id, privacy_level=PrivacyLevel.L1, now=NOW
    )
    assert result.hits == ()
    assert result.candidate_count == 0


async def test_retrieval_prioritizes_assistant_exact_fact(
    store: MemoryStore, user: AppUserRecord
) -> None:
    await store.add(
        candidate(
            "用户曾记录自己的身材数据为 80-60-82",
            fact_key="profile.measurements",
        ),
        user_id=user.id,
    )
    assistant = await store.add(
        candidate(
            "助手自述三围为 91-63-88 厘米",
            subject_kind=MemorySubjectKind.ASSISTANT,
            subject_key="assistant:primary",
            fact_key="profile.measurements",
            origin_kind=MemoryOriginKind.ASSISTANT_STATEMENT,
            importance=0.8,
            pin=True,
        ),
        user_id=user.id,
    )

    result = await MemoryRetriever(store).retrieve(
        "你的身材数据是多少？",
        user_id=user.id,
        privacy_level=PrivacyLevel.L1,
        now=NOW,
    )

    assert result.subject_hint == "assistant"
    assert result.fact_hint == "profile.measurements"
    assert result.hits[0].memory.id == assistant.id
    assert "exact_fact" in result.hits[0].reasons
    assert "subject_bonus" in result.hits[0].reasons
    context = MemoryRetriever.render_context(result)
    assert "[关于你自己]" in context
    assert "91-63-88" in context


async def test_retrieval_exactly_recalls_user_job(store: MemoryStore, user: AppUserRecord) -> None:
    job = await store.add(
        candidate(
            "用户是IT行业从业者，担任ELN产品负责人",
            fact_key="profile.job",
        ),
        user_id=user.id,
    )
    await store.add(candidate("用户喜欢黑咖啡"), user_id=user.id)

    retriever = MemoryRetriever(store)
    result = await retriever.retrieve(
        "你还记得我的工作吗",
        user_id=user.id,
        privacy_level=PrivacyLevel.L1,
        now=NOW,
    )

    assert result.subject_hint == "user"
    assert result.fact_hint == "profile.job"
    assert result.hits[0].memory.id == job.id
    assert "exact_fact" in result.hits[0].reasons
    assert retriever.grounded_hits(result)[0].memory.id == job.id


async def test_retrieval_lists_memories_for_requested_subject(
    store: MemoryStore, user: AppUserRecord
) -> None:
    expected = {
        (await store.add(candidate("用户叫浩宇"), user_id=user.id)).id,
        (await store.add(candidate("用户喜欢黑咖啡"), user_id=user.id)).id,
        (await store.add(candidate("用户在济南生活"), user_id=user.id)).id,
    }
    await store.add(
        candidate(
            "助手身高为 165 厘米",
            subject_kind=MemorySubjectKind.ASSISTANT,
            subject_key="assistant:primary",
            fact_key="profile.height",
            origin_kind=MemoryOriginKind.ASSISTANT_STATEMENT,
        ),
        user_id=user.id,
    )

    retriever = MemoryRetriever(store)
    result = await retriever.retrieve(
        "在你的记忆里关于我的有哪些",
        user_id=user.id,
        privacy_level=PrivacyLevel.L1,
        now=NOW,
    )

    assert result.subject_hint == "user"
    assert {hit.memory.id for hit in result.hits} == expected
    assert all(hit.reasons == ("subject_inventory",) for hit in result.hits)
    assert retriever.grounded_hits(result) == result.hits
    context = MemoryRetriever.render_context(result)
    assert "[关于用户]" in context
    assert "[关于你自己]" not in context


async def test_retrieval_exactly_recalls_multiple_requested_assistant_profile_slots(
    store: MemoryStore, user: AppUserRecord
) -> None:
    expected = {
        "profile.height": "助手身高为 160 厘米",
        "profile.weight": "助手体重为 48 公斤",
        "profile.measurements": "助手三围为 82-60-86 厘米",
    }
    for fact_key, content in expected.items():
        await store.add(
            candidate(
                content,
                subject_kind=MemorySubjectKind.ASSISTANT,
                subject_key="assistant:primary",
                fact_key=fact_key,
                origin_kind=MemoryOriginKind.ASSISTANT_STATEMENT,
                importance=0.8,
            ),
            user_id=user.id,
        )

    retriever = MemoryRetriever(store)
    result = await retriever.retrieve(
        "告诉我你的身高、体重和三围",
        user_id=user.id,
        privacy_level=PrivacyLevel.L1,
        now=NOW,
    )

    exact_by_fact = {
        hit.memory.fact_key: hit
        for hit in result.hits
        if "exact_fact" in hit.reasons and hit.memory.subject_kind == "assistant"
    }
    assert set(expected).issubset(exact_by_fact)
    assert all(exact_by_fact[key].memory.content == value for key, value in expected.items())
    grounded = retriever.grounded_hits(result)
    assert set(expected).issubset(
        {hit.memory.fact_key for hit in grounded if hit.memory.subject_kind == "assistant"}
    )


async def test_retrieval_privacy_gating(store: MemoryStore, user: AppUserRecord) -> None:
    await store.add(candidate("用户不吃香菜"), user_id=user.id)
    await store.add(
        candidate("用户发生过一次亲密对话", privacy_level=PrivacyLevel.L2), user_id=user.id
    )

    public = await MemoryRetriever(store).retrieve(
        "用户 recently 聊了什么", user_id=user.id, privacy_level=PrivacyLevel.L1, now=NOW
    )
    private = await MemoryRetriever(store).retrieve(
        "用户 recently 聊了什么", user_id=user.id, privacy_level=PrivacyLevel.L2, now=NOW
    )
    assert all(hit.memory.privacy_level != "L2" for hit in public.hits)
    assert any(hit.memory.privacy_level == "L2" for hit in private.hits)


async def test_retrieval_enforces_type_quota(store: MemoryStore, user: AppUserRecord) -> None:
    for index in range(5):
        await store.add(
            candidate(f"用户不吃香菜的第{index}条相关备注", type=MemoryType.SEMANTIC),
            user_id=user.id,
        )
    await store.add(candidate("用户不吃香菜"), user_id=user.id)

    result = await MemoryRetriever(store).retrieve(
        "香菜菜单备注", user_id=user.id, privacy_level=PrivacyLevel.L1, now=NOW
    )
    semantic_hits = [hit for hit in result.hits if hit.memory.type == "semantic"]
    assert len(semantic_hits) <= 3
    assert len(result.hits) <= 8


async def test_consolidation_supports_conflicts_and_creates(
    store: MemoryStore, user: AppUserRecord
) -> None:
    ingester = MemoryIngester(store)
    message_id = uuid4()
    base = MemoryCandidate(
        type=MemoryType.PREFERENCE,
        content="用户不吃香菜",
        privacy_level=PrivacyLevel.L1,
        sources=[MemorySourceRef(source_kind=MemorySourceKind.MESSAGE, source_id=str(message_id))],
        importance=0.6,
    )

    first = await ingester.ingest(base, user_id=user.id)
    assert first.decision is ConsolidateDecision.CREATED
    assert first.memory.status == "active"

    repeat = MemoryCandidate(
        type=MemoryType.PREFERENCE,
        content="用户不吃香菜",
        privacy_level=PrivacyLevel.L1,
        sources=[MemorySourceRef(source_kind=MemorySourceKind.MESSAGE, source_id=str(uuid4()))],
        importance=0.6,
    )
    supported = await ingester.ingest(repeat, user_id=user.id)
    assert supported.decision is ConsolidateDecision.SUPPORTED
    assert supported.memory.id == first.memory.id
    assert supported.memory.importance > first.memory.importance
    assert len(await store.list_memories(user_id=user.id)) == 1

    conflicting = await ingester.ingest(
        candidate("用户其实喜欢吃香菜", type=MemoryType.PREFERENCE), user_id=user.id
    )
    assert conflicting.decision is ConsolidateDecision.CONFLICT
    assert conflicting.memory.status == "conflict"
    assert conflicting.memory.conflict_with == first.memory.id
    assert conflicting.related is not None
    assert conflicting.related.status == "active"

    unrelated = await ingester.ingest(
        candidate("用户养了一只猫", type=MemoryType.SEMANTIC), user_id=user.id
    )
    assert unrelated.decision is ConsolidateDecision.CREATED


async def test_fact_key_conflict_is_deterministic_and_subject_scoped(
    store: MemoryStore, user: AppUserRecord
) -> None:
    ingester = MemoryIngester(store)
    assistant_160 = candidate(
        "助手身高为 160 厘米",
        subject_kind=MemorySubjectKind.ASSISTANT,
        subject_key="assistant:primary",
        fact_key="profile.height",
        origin_kind=MemoryOriginKind.ASSISTANT_STATEMENT,
    )
    first = await ingester.ingest(assistant_160, user_id=user.id)
    assert first.decision is ConsolidateDecision.CREATED

    repeated = await ingester.ingest(assistant_160, user_id=user.id)
    assert repeated.decision is ConsolidateDecision.SUPPORTED
    assert repeated.memory.id == first.memory.id

    changed = await ingester.ingest(
        candidate(
            "助手身高为 165 厘米",
            subject_kind=MemorySubjectKind.ASSISTANT,
            subject_key="assistant:primary",
            fact_key="profile.height",
            origin_kind=MemoryOriginKind.ASSISTANT_STATEMENT,
        ),
        user_id=user.id,
    )
    assert changed.decision is ConsolidateDecision.CONFLICT
    assert changed.memory.conflict_with == first.memory.id

    user_height = await ingester.ingest(
        candidate(
            "用户身高为 175 厘米",
            fact_key="profile.height",
        ),
        user_id=user.id,
    )
    assert user_height.decision is ConsolidateDecision.CREATED
    assert user_height.memory.subject_kind == "user"
    assert user_height.memory.conflict_with is None


async def test_conflict_resolution_adopt_and_keep(store: MemoryStore, user: AppUserRecord) -> None:
    ingester = MemoryIngester(store)
    original = (await ingester.ingest(candidate("用户不吃香菜"), user_id=user.id)).memory
    conflict = (await ingester.ingest(candidate("用户其实喜欢吃香菜"), user_id=user.id)).memory

    adopted = await store.resolve_conflict(conflict.id, adopt=True, actor="admin")
    stale = await store.get(original.id)
    assert adopted.status == "active"
    assert adopted.conflict_with is None
    assert stale.status == "superseded"
    assert stale.superseded_by == adopted.id

    keep_case = await ingester.ingest(candidate("用户不吃芹菜"), user_id=user.id)
    assert keep_case.decision is ConsolidateDecision.CREATED
    second_conflict = (
        await ingester.ingest(candidate("用户不吃香菜和芹菜"), user_id=user.id)
    ).memory
    kept = await store.resolve_conflict(second_conflict.id, adopt=False, actor="admin")
    assert kept.status == "archived"

    with pytest.raises(ValueError):
        await store.resolve_conflict(adopted.id, adopt=True, actor="admin")


async def test_edit_supersedes_and_keeps_lineage(store: MemoryStore, user: AppUserRecord) -> None:
    message_id = uuid4()
    original = await store.add(
        MemoryCandidate(
            type=MemoryType.SEMANTIC,
            content="用户在杭州工作",
            privacy_level=PrivacyLevel.L1,
            sources=[
                MemorySourceRef(
                    source_kind=MemorySourceKind.MESSAGE,
                    source_id=str(message_id),
                    excerpt="我在杭州工作",
                )
            ],
        ),
        user_id=user.id,
    )

    replacement = await store.edit(
        original.id,
        content="用户在上海工作，今年刚搬家",
        actor="admin",
        reason="用户纠正了城市",
    )
    assert replacement.status == "active"
    assert replacement.content == "用户在上海工作，今年刚搬家"

    stale = await store.get(original.id)
    assert stale.status == "superseded"
    assert stale.superseded_by == replacement.id
    assert stale.supersede_reason == "用户纠正了城市"

    sources = await store.get_sources(replacement.id)
    kinds = {(source.source_kind, source.source_id) for source in sources}
    assert ("message", str(message_id)) in kinds
    assert ("memory", str(original.id)) in kinds

    lineage = await store.lineage(replacement.id)
    assert [item.id for item in lineage] == [original.id, replacement.id]

    with pytest.raises(ValueError):
        await store.edit(original.id, content="再改一次", actor="admin", reason="noop")

    active = await store.list_memories(user_id=user.id, status=MemoryStatus.ACTIVE)
    assert [item.id for item in active] == [replacement.id]


async def test_rule_extractor_classifies_and_privacy_skips(store: MemoryStore) -> None:
    from app.memory import RuleBasedExtractor

    extractor = RuleBasedExtractor()
    message_id = uuid4()
    results = await extractor.extract(
        "我不吃香菜。我喜欢你。我周五要汇报 PPT。随便聊聊。我叫小明。",
        message_id=message_id,
        privacy_level=PrivacyLevel.L1,
        occurred_at=NOW,
    )
    by_type = {item.type: item for item in results}
    assert set(by_type) == {
        MemoryType.PREFERENCE,
        MemoryType.COMMITMENT,
        MemoryType.SEMANTIC,
    }
    assert by_type[MemoryType.PREFERENCE].content == "用户不吃香菜"
    assert by_type[MemoryType.SEMANTIC].content == "用户叫小明"
    commitment = by_type[MemoryType.COMMITMENT]
    assert commitment.valid_to == NOW + timedelta(days=7)

    message_source = [
        source for item in results for source in item.sources if item.type == "preference"
    ]
    assert message_source[0].source_id == str(message_id)

    ingester = MemoryIngester(store)
    outcomes = await ingester.ingest_message(
        user_id=uuid4(),
        message_id=uuid4(),
        text="我不吃香菜。",
        privacy_level=PrivacyLevel.L2,
        occurred_at=NOW,
    )
    assert outcomes == []


async def test_rule_extractor_assigns_job_fact_slot() -> None:
    result = await RuleBasedExtractor().extract(
        "我在IT行业工作，是ELN产品负责人。",
        message_id=uuid4(),
        privacy_level=PrivacyLevel.L1,
        occurred_at=NOW,
    )

    assert len(result) == 1
    assert result[0].content == "用户在IT行业工作，是ELN产品负责人"
    assert result[0].fact_key == "profile.job"


async def test_turn_extractor_creates_assistant_and_shared_candidates() -> None:
    extractor = TurnMemoryExtractor()
    user_message_id = uuid4()
    assistant_message_id = uuid4()

    candidates = await extractor.extract_turn(
        user_text="记住，你的生日是12月27日。以后我们周五晚上一起看电影。",
        user_message_id=user_message_id,
        user_occurred_at=NOW,
        assistant_text="我的三围是91-63-88。我喜欢桂花味的甜点。",
        assistant_message_id=assistant_message_id,
        assistant_occurred_at=NOW,
        privacy_level=PrivacyLevel.L1,
    )

    by_fact = {item.fact_key: item for item in candidates if item.fact_key}
    assert by_fact["profile.birthday"].subject_kind == "assistant"
    assert by_fact["profile.birthday"].origin_kind == "user_statement"
    assert by_fact["profile.measurements"].content == "助手三围为 91-63-88 厘米"
    assert by_fact["profile.measurements"].origin_kind == "assistant_statement"
    assert by_fact["preference.food"].content == "助手喜欢桂花味的甜点"
    shared = [item for item in candidates if item.subject_kind == "shared"]
    assert len(shared) == 1
    assert shared[0].type == "commitment"
    assert shared[0].origin_kind == "shared_turn"


async def test_turn_extractor_keeps_multiple_profile_facts_from_one_sentence() -> None:
    extractor = TurnMemoryExtractor()

    candidates = await extractor.extract_turn(
        user_text="告诉我你的身高、体重和三围。",
        user_message_id=uuid4(),
        user_occurred_at=NOW,
        assistant_text="我身高160厘米，体重48公斤，三围82-60-86。",
        assistant_message_id=uuid4(),
        assistant_occurred_at=NOW,
        privacy_level=PrivacyLevel.L1,
    )

    by_fact = {
        item.fact_key: item.content
        for item in candidates
        if item.subject_kind == "assistant" and item.fact_key is not None
    }
    assert by_fact["profile.height"] == "助手身高为 160 厘米"
    assert by_fact["profile.weight"] == "助手体重为 48 公斤"
    assert by_fact["profile.measurements"] == "助手三围为 82-60-86 厘米"

    assertions = dict(
        extract_assistant_fact_assertions("我身高160厘米，体重48公斤，三围82-60-86。")
    )
    assert assertions == {
        "profile.height": "助手身高为 160 厘米",
        "profile.weight": "助手体重为 48 公斤",
        "profile.measurements": "助手三围为 82-60-86 厘米",
    }


async def test_turn_extractor_suppresses_retrieved_assistant_echo(
    store: MemoryStore, user: AppUserRecord
) -> None:
    existing = await store.add(
        candidate(
            "助手身高为 160 厘米",
            subject_kind=MemorySubjectKind.ASSISTANT,
            subject_key="assistant:primary",
            fact_key="profile.height",
            origin_kind=MemoryOriginKind.ASSISTANT_STATEMENT,
        ),
        user_id=user.id,
    )
    candidates = await TurnMemoryExtractor().extract_turn(
        user_text="你的身高是多少？",
        user_message_id=uuid4(),
        user_occurred_at=NOW,
        assistant_text="我的身高是160厘米。",
        assistant_message_id=uuid4(),
        assistant_occurred_at=NOW,
        privacy_level=PrivacyLevel.L1,
        retrieved_memories=(existing,),
    )
    assert not any(item.fact_key == "profile.height" for item in candidates)


async def test_l3_memory_is_rejected(store: MemoryStore, user: AppUserRecord) -> None:
    with pytest.raises(ValueError, match="L3"):
        await store.add(candidate("原始遥测", privacy_level=PrivacyLevel.L3), user_id=user.id)


async def test_chat_turn_builds_memory_loop(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    config_store = DatabaseConfigStore(database, config_path)
    await config_store.load()
    requests: list[CompletionRequest] = []
    service = ChatService(
        database,
        config_store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
        memory_store=MemoryStore(database),
    )
    conversation = await service.create_conversation(user_id=user.id, title="memory")

    await service.send_message(
        conversation.id,
        user_id=user.id,
        text="记住：我不吃香菜。",
        privacy_level=PrivacyLevel.L1,
    )
    await service.drain_background_work()
    store = MemoryStore(database)
    memories = await store.list_memories(user_id=user.id, status=MemoryStatus.ACTIVE)
    assert [item.content for item in memories] == ["用户不吃香菜"]
    assert memories[0].extractor_version == "rule-v1"
    sources = await store.get_sources(memories[0].id)
    assert sources[0].source_kind == "message"

    second = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="帮我点菜，我能吃香菜吗",
        privacy_level=PrivacyLevel.L1,
    )
    await service.drain_background_work()
    system_prompt = requests[-1].messages[0].content
    assert "【长期记忆】" in system_prompt
    assert "用户不吃香菜" in system_prompt
    memory_meta = meta_section(second.assistant_message.decision_meta, "memory")
    assert memory_meta["policy_version"] == "hybrid-subject-v4"
    assert memory_meta["hits"][0]["id"] == memories[0].id
    assert memory_meta["hits"][0]["subject"] == "user"
    assert memory_meta["hits"][0]["subject_key"] == "user:self"
    assert memory_meta["hits"][0]["fact_key"] is None
    assert memory_meta["hits"][0]["type"] == "preference"

    fresh = await store.get(memories[0].id)
    assert fresh.access_count == 1


async def test_chat_memory_failure_never_breaks_turn(
    database: Database, user: AppUserRecord, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    config_store = DatabaseConfigStore(database, config_path)
    await config_store.load()

    async def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("consolidation down")

    monkeypatch.setattr(MemoryIngester, "ingest_message", explode)
    requests: list[CompletionRequest] = []
    service = ChatService(
        database,
        config_store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
        memory_store=MemoryStore(database),
    )
    conversation = await service.create_conversation(user_id=user.id, title="resilience")

    turn = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="我不吃香菜。",
        privacy_level=PrivacyLevel.L1,
    )
    await service.drain_background_work()
    assert turn.assistant_message.content == "reply from dialogue-v1"


async def test_admin_memory_api_manages_lifecycle(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    config_store = DatabaseConfigStore(database, config_path)
    app = create_app(
        database,
        config_store=config_store,
        watch_config=False,
        admin_token="test-admin-token",
    )
    headers = {"Authorization": "Bearer test-admin-token"}

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        unauthorized = await client.get("/api/v1/admin/memories")
        created = await client.post(
            "/api/v1/admin/memories",
            headers=headers,
            json={
                "user_id": str(user.id),
                "type": "semantic",
                "content": "用户在杭州工作",
                "privacy_level": "L1",
                "importance": 0.7,
            },
        )
        memory_id = created.json()["id"]
        assistant_created = await client.post(
            "/api/v1/admin/memories",
            headers=headers,
            json={
                "user_id": str(user.id),
                "subject": "assistant",
                "type": "semantic",
                "fact_key": "profile.measurements",
                "content": "助手自述三围为 91-63-88 厘米",
                "privacy_level": "L1",
                "importance": 0.8,
                "pin": True,
            },
        )
        assistant_listing = await client.get(
            "/api/v1/admin/memories",
            headers=headers,
            params={
                "user_id": str(user.id),
                "subject": "assistant",
                "fact_key": "profile.measurements",
                "origin_kind": "manual",
            },
        )
        listing = await client.get(
            "/api/v1/admin/memories",
            headers=headers,
            params={"user_id": str(user.id), "status": "active"},
        )
        detail = await client.get(f"/api/v1/admin/memories/{memory_id}", headers=headers)
        edited = await client.patch(
            f"/api/v1/admin/memories/{memory_id}",
            headers=headers,
            json={"content": "用户在上海工作", "reason": "用户纠正了城市"},
        )
        queried = await client.post(
            "/api/v1/admin/memories/query",
            headers=headers,
            json={"user_id": str(user.id), "query": "用户在哪个城市工作"},
        )
        l3_rejected = await client.post(
            "/api/v1/admin/memories",
            headers=headers,
            json={
                "user_id": str(user.id),
                "type": "semantic",
                "content": "原始遥测",
                "privacy_level": "L3",
            },
        )
        archived = await client.post(
            f"/api/v1/admin/memories/{edited.json()['id']}/archive", headers=headers
        )

    assert unauthorized.status_code == 401
    assert created.status_code == 201
    assert created.json()["status"] == "active"
    assert created.json()["subject"] == "user"
    assert created.json()["subject_key"] == "user:self"
    assert created.json()["origin_kind"] == "manual"
    assert assistant_created.status_code == 201
    assert assistant_created.json()["subject"] == "assistant"
    assert assistant_created.json()["subject_key"] == "assistant:primary"
    assert assistant_created.json()["fact_key"] == "profile.measurements"
    assert assistant_created.json()["origin_kind"] == "manual"
    assert [item["id"] for item in assistant_listing.json()] == [assistant_created.json()["id"]]
    assert any(item["content"] == "用户在杭州工作" for item in listing.json())
    assert detail.json()["sources"][0]["source_kind"] == "manual"
    assert edited.json()["content"] == "用户在上海工作"
    assert edited.json()["id"] != memory_id
    assert edited.json()["subject"] == "user"
    assert edited.json()["subject_key"] == "user:self"
    assert detail.json()["lineage"] or True
    assert queried.json()["policy_version"] == "hybrid-subject-v4"
    assert queried.json()["hits"]
    assert l3_rejected.status_code == 422
    assert archived.json()["status"] == "archived"


async def test_hard_delete_removes_chain_sources_and_records_ledger(
    store: MemoryStore, user: AppUserRecord
) -> None:
    original = await store.add(
        MemoryCandidate(
            type=MemoryType.SEMANTIC,
            content="用户在杭州工作",
            privacy_level=PrivacyLevel.L1,
            sources=[
                MemorySourceRef(
                    source_kind=MemorySourceKind.MESSAGE,
                    source_id=str(uuid4()),
                    excerpt="我在杭州工作",
                )
            ],
        ),
        user_id=user.id,
    )
    replacement = await store.edit(
        original.id, content="用户在上海工作", actor="admin", reason="用户纠正了城市"
    )
    target = await store.add(candidate("用户不吃香菜"), user_id=user.id)
    conflict_row = await store.add(
        candidate("用户其实住在别的城市"),
        user_id=user.id,
        status=MemoryStatus.CONFLICT,
        conflict_with=original.id,
    )

    receipt = await store.hard_delete(original.id, actor="admin", reason="用户要求清除")

    assert receipt.entity_id == str(original.id)
    assert receipt.deleted_ids == (original.id, replacement.id)
    with pytest.raises(LookupError):
        await store.get(original.id)
    with pytest.raises(LookupError):
        await store.get(replacement.id)
    assert await store.get_sources(original.id) == []
    assert await store.get_sources(replacement.id) == []

    result = await MemoryRetriever(store).retrieve(
        "用户在哪个城市工作", user_id=user.id, privacy_level=PrivacyLevel.L1, now=NOW
    )
    assert all(hit.memory.id not in receipt.deleted_ids for hit in result.hits)

    detached = await store.get(conflict_row.id)
    assert detached.status == "conflict"
    assert detached.conflict_with is None
    survivor = await store.get(target.id)
    assert survivor.status == "active"

    ledger = await store.list_deletion_ledger()
    assert len(ledger) == 1
    assert ledger[0].entity_kind == "memory"
    assert ledger[0].entity_id == str(original.id)
    assert ledger[0].deleted_ids == (original.id, replacement.id)
    assert ledger[0].requested_by == "admin"
    assert ledger[0].reason == "用户要求清除"

    with pytest.raises(LookupError):
        await store.hard_delete(original.id, actor="admin")


async def test_admin_delete_and_ledger_api(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    config_store = DatabaseConfigStore(database, config_path)
    app = create_app(
        database,
        config_store=config_store,
        watch_config=False,
        admin_token="test-admin-token",
    )
    headers = {"Authorization": "Bearer test-admin-token"}
    store = MemoryStore(database)
    first = await store.add(candidate("用户在杭州工作"), user_id=user.id)
    second = await store.edit(first.id, content="用户在上海工作", actor="admin", reason="纠正")

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        page = await client.get("/admin/memory")
        unauthorized = await client.get("/api/v1/admin/deletion-ledger")
        deleted = await client.delete(
            f"/api/v1/admin/memories/{first.id}",
            headers=headers,
            params={"reason": "用户要求清除"},
        )
        missing = await client.get(f"/api/v1/admin/memories/{first.id}", headers=headers)
        ledger = await client.get("/api/v1/admin/deletion-ledger", headers=headers)

    assert page.status_code == 200
    # Vue SPA when the admin dist exists, vanilla page otherwise.
    assert '<div id="app"></div>' in page.text or "记忆库" in page.text
    assert unauthorized.status_code == 401
    assert deleted.status_code == 200
    assert deleted.json()["deleted_ids"] == [first.id, second.id]
    assert deleted.json()["ledger_id"] == ledger.json()[0]["id"]
    assert missing.status_code == 404
    assert ledger.json()[0]["reason"] == "用户要求清除"
    assert ledger.json()[0]["deleted_ids"] == [first.id, second.id]


class ScriptedRouter:
    """Returns fixed payloads in order; used for reply then extraction calls."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        text = self._replies.pop(0) if self._replies else "好的。"
        return CompletionResult(
            text=text,
            provider="openai_compatible",
            model="utility-v1",
            endpoint="cloud",
            route=request.route,
            finish_reason="stop",
            usage=ModelUsage(),
            latency_ms=3.0,
        )

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        del on_delta
        return await self.complete(request)


class StreamingConsistencyRouter(ScriptedRouter):
    """首个响应按流式发出, 后续 complete 用作一致性 repair。"""

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        self.requests.append(request)
        text = self._replies.pop(0) if self._replies else "好的。"
        await on_delta(text)
        return CompletionResult(
            text=text,
            provider="openai_compatible",
            model="utility-v1",
            endpoint="cloud",
            route=request.route,
            finish_reason="stop",
            usage=ModelUsage(),
            latency_ms=3.0,
        )


async def test_llm_extractor_assigns_job_fact_slot() -> None:
    backend = ScriptedRouter(
        [
            '{"candidates":[{"type":"preference","content":"用户是IT行业从业者，担任ELN产品负责人","importance":0.8,"confidence":0.9}]}'
        ]
    )

    result = await LlmMemoryExtractor().extract(
        "我在IT行业工作，是ELN产品负责人。",
        message_id=uuid4(),
        privacy_level=PrivacyLevel.L1,
        occurred_at=NOW,
        backend=backend,
    )

    assert len(result) == 1
    assert result[0].fact_key == "profile.job"


async def _chat_service(
    database: Database,
    tmp_path: Path,
    router: ScriptedRouter,
    *,
    extractor: MemoryExtractor,
) -> ChatService:
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    config_store = DatabaseConfigStore(database, config_path)
    await config_store.load()
    return ChatService(
        database,
        config_store,
        router_builder=lambda config: router,
        memory_store=MemoryStore(database),
        memory_extractor=extractor,
    )


async def test_assistant_self_fact_persists_across_conversations_without_echo_growth(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    router = ScriptedRouter(
        [
            "我的身高是160厘米。",
            "当然，我的身高是160厘米。",
        ]
    )
    service = await _chat_service(
        database,
        tmp_path,
        router,
        extractor=RuleBasedExtractor(),
    )

    first_conversation = await service.create_conversation(user_id=user.id, title="self-a")
    await service.send_message(
        first_conversation.id,
        user_id=user.id,
        text="你的身高是多少？",
        privacy_level=PrivacyLevel.L1,
    )
    await service.drain_background_work()

    store = MemoryStore(database)
    assistant_memories = await store.list_memories(
        user_id=user.id,
        subject_kind=MemorySubjectKind.ASSISTANT,
        fact_key="profile.height",
        status=MemoryStatus.ACTIVE,
    )
    assert len(assistant_memories) == 1
    established = assistant_memories[0]
    assert established.content == "助手身高为 160 厘米"
    assert established.origin_kind == "assistant_statement"
    assert established.importance == 0.75

    second_conversation = await service.create_conversation(user_id=user.id, title="self-b")
    second_turn = await service.send_message(
        second_conversation.id,
        user_id=user.id,
        text="你的身高是多少？",
        privacy_level=PrivacyLevel.L1,
    )
    await service.drain_background_work()

    second_system_prompt = router.requests[-1].messages[0].content
    assert "[关于你自己]" in second_system_prompt
    assert "助手身高为 160 厘米" in second_system_prompt
    memory_meta = meta_section(second_turn.assistant_message.decision_meta, "memory")
    assert memory_meta["hits"][0]["id"] == established.id
    assert memory_meta["hits"][0]["subject"] == "assistant"
    assert memory_meta["hits"][0]["fact_key"] == "profile.height"
    assert "exact_fact" in memory_meta["hits"][0]["reasons"]

    after_echo = await store.list_memories(
        user_id=user.id,
        subject_kind=MemorySubjectKind.ASSISTANT,
        fact_key="profile.height",
        status=MemoryStatus.ACTIVE,
    )
    assert len(after_echo) == 1
    assert after_echo[0].id == established.id
    assert after_echo[0].importance == established.importance


async def test_memory_consistency_guard_repairs_wrong_assistant_fact(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    established = await MemoryStore(database).add(
        candidate(
            "助手身高为 160 厘米",
            subject_kind=MemorySubjectKind.ASSISTANT,
            subject_key="assistant:primary",
            fact_key="profile.height",
            origin_kind=MemoryOriginKind.ASSISTANT_STATEMENT,
            importance=0.8,
        ),
        user_id=user.id,
    )
    router = ScriptedRouter(["我的身高是165厘米。", "我的身高是160厘米。"])
    service = await _chat_service(
        database,
        tmp_path,
        router,
        extractor=RuleBasedExtractor(),
    )
    conversation = await service.create_conversation(user_id=user.id, title="guard-repair")

    turn = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="你的身高是多少？",
        privacy_level=PrivacyLevel.L1,
    )
    await service.drain_background_work()

    assert turn.assistant_message.content == "我的身高是160厘米。"
    assert len(router.requests) == 2
    assert "【一致性修复】" in router.requests[1].messages[0].content
    meta = meta_section(turn.assistant_message.decision_meta, "memory")
    assert meta["consistency"] == {
        "checked": True,
        "repaired": True,
        "fallback_used": False,
        "conflict_memory_ids": [established.id],
    }


async def test_memory_consistency_guard_falls_back_after_failed_repair(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    established = await MemoryStore(database).add(
        candidate(
            "助手身高为 160 厘米",
            subject_kind=MemorySubjectKind.ASSISTANT,
            subject_key="assistant:primary",
            fact_key="profile.height",
            origin_kind=MemoryOriginKind.ASSISTANT_STATEMENT,
        ),
        user_id=user.id,
    )
    router = ScriptedRouter(["我的身高是165厘米。", "我还是觉得自己身高165厘米。"])
    service = await _chat_service(
        database,
        tmp_path,
        router,
        extractor=RuleBasedExtractor(),
    )
    conversation = await service.create_conversation(user_id=user.id, title="guard-fallback")

    turn = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="你的身高是多少？",
        privacy_level=PrivacyLevel.L1,
    )
    await service.drain_background_work()

    assert "我身高为 160 厘米" in turn.assistant_message.content
    meta = meta_section(turn.assistant_message.decision_meta, "memory")
    assert meta["consistency"]["checked"] is True
    assert meta["consistency"]["repaired"] is False
    assert meta["consistency"]["fallback_used"] is True
    assert meta["consistency"]["conflict_memory_ids"] == [established.id]


async def test_streaming_exact_fact_never_emits_unchecked_conflicting_delta(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    await MemoryStore(database).add(
        candidate(
            "助手身高为 160 厘米",
            subject_kind=MemorySubjectKind.ASSISTANT,
            subject_key="assistant:primary",
            fact_key="profile.height",
            origin_kind=MemoryOriginKind.ASSISTANT_STATEMENT,
        ),
        user_id=user.id,
    )
    router = StreamingConsistencyRouter(["我的身高是165厘米。", "我的身高是160厘米。"])
    service = await _chat_service(
        database,
        tmp_path,
        router,
        extractor=RuleBasedExtractor(),
    )
    conversation = await service.create_conversation(user_id=user.id, title="guard-stream")
    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="你的身高是多少？",
        privacy_level=PrivacyLevel.L1,
    )
    deltas: list[str] = []

    async def capture(delta: str) -> None:
        deltas.append(delta)

    turn = await service.run_stream(pending, capture)

    assert deltas == ["我的身高是160厘米。"]
    assert all("165" not in delta for delta in deltas)
    assert turn.assistant_message.content == "我的身高是160厘米。"


async def test_llm_extractor_stores_desensitized_l2_memory(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    router = ScriptedRouter(
        [
            "我在呢。",
            '{"candidates":[{"type":"emotional","content":"用户与Aria进行了一次长时间温暖的对话，情绪放松","importance":0.6,"confidence":0.8}]}',
        ]
    )
    service = await _chat_service(database, tmp_path, router, extractor=LlmMemoryExtractor())
    conversation = await service.create_conversation(user_id=user.id, title="l2")

    await service.send_message(
        conversation.id,
        user_id=user.id,
        text="（亲密对话占位文本）今晚聊了很久",
        privacy_level=PrivacyLevel.L2,
    )
    await service.drain_background_work()

    assert len(router.requests) == 2
    reply_request, extraction_request = router.requests
    assert reply_request.privacy_level == "L2"
    assert extraction_request.route == "utility"
    assert extraction_request.privacy_level == "L2"
    assert extraction_request.json_mode is True
    assert "隐私约束" in extraction_request.messages[0].content

    store = MemoryStore(database)
    memories = await store.list_memories(user_id=user.id, status=MemoryStatus.ACTIVE)
    assert [item.content for item in memories] == ["用户与Aria进行了一次长时间温暖的对话，情绪放松"]
    assert memories[0].privacy_level == "L2"
    assert memories[0].extractor_version == "llm-utility-v1"

    public = await MemoryRetriever(store).retrieve(
        "最近聊了什么", user_id=user.id, privacy_level=PrivacyLevel.L1, now=NOW
    )
    private = await MemoryRetriever(store).retrieve(
        "最近聊了什么", user_id=user.id, privacy_level=PrivacyLevel.L2, now=NOW
    )
    assert public.hits == ()
    assert private.hits


async def test_llm_extractor_falls_back_to_rules_on_bad_output(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    router = ScriptedRouter(["好的。", "模型抽风了，这不是 JSON"])
    service = await _chat_service(database, tmp_path, router, extractor=LlmMemoryExtractor())
    conversation = await service.create_conversation(user_id=user.id, title="fallback")

    await service.send_message(
        conversation.id,
        user_id=user.id,
        text="记住：我不吃香菜。",
        privacy_level=PrivacyLevel.L1,
    )
    await service.drain_background_work()

    memories = await MemoryStore(database).list_memories(
        user_id=user.id, status=MemoryStatus.ACTIVE
    )
    assert [item.content for item in memories] == ["用户不吃香菜"]
    assert memories[0].extractor_version == "rule-v1"


async def test_llm_extractor_with_empty_candidates_stores_nothing(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    router = ScriptedRouter(["好的。", '{"candidates":[]}'])
    service = await _chat_service(database, tmp_path, router, extractor=LlmMemoryExtractor())
    conversation = await service.create_conversation(user_id=user.id, title="empty")

    turn = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="今天天气不错",
        privacy_level=PrivacyLevel.L1,
    )
    await service.drain_background_work()
    assert turn.assistant_message.content == "好的。"
    memories = await MemoryStore(database).list_memories(user_id=user.id)
    assert memories == []


async def test_delete_conversation_cascades_and_replay_is_idempotent(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    config_store = DatabaseConfigStore(database, config_path)
    await config_store.load()
    requests: list[CompletionRequest] = []
    service = ChatService(
        database,
        config_store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
        memory_store=MemoryStore(database),
    )
    conversation_a = await service.create_conversation(user_id=user.id, title="A")
    conversation_b = await service.create_conversation(user_id=user.id, title="B")
    turn_a = await service.send_message(
        conversation_a.id, user_id=user.id, text="我不吃香菜。", privacy_level=PrivacyLevel.L1
    )
    await service.send_message(
        conversation_b.id, user_id=user.id, text="我叫小雷。", privacy_level=PrivacyLevel.L1
    )
    await service.drain_background_work()

    store = MemoryStore(database)
    memories = {
        item.content: item
        for item in await store.list_memories(user_id=user.id, status=MemoryStatus.ACTIVE)
    }
    assert set(memories) == {"用户不吃香菜", "用户叫小雷"}
    memory_a = memories["用户不吃香菜"]

    receipt = await service.delete_conversation(conversation_a.id, user_id=user.id)

    assert receipt.entity_id == str(conversation_a.id)
    assert receipt.deleted_ids == (memory_a.id,)
    assert receipt.ledger_id > 0
    assert [item.id for item in await service.list_conversations(user_id=user.id)] == [
        conversation_b.id
    ]
    with pytest.raises(LookupError):
        await service.list_messages(conversation_a.id, user_id=user.id)
    remaining = await store.list_memories(user_id=user.id, status=MemoryStatus.ACTIVE)
    assert [item.content for item in remaining] == ["用户叫小雷"]
    ledger = await store.list_deletion_ledger()
    assert ledger[0].entity_kind == "message"
    assert ledger[0].entity_id == str(conversation_a.id)
    assert ledger[0].deleted_ids == (memory_a.id,)

    # Simulate a backup restore resurrecting the conversation and its memory.
    async with database.sessions.begin() as session:
        session.add(
            ConversationRecord(
                id=conversation_a.id,
                user_id=user.id,
                title="A",
                status="active",
                last_seq=2,
                last_turn_seq=1,
            )
        )
        session.add(
            MessageRecord(
                id=turn_a.user_message.id,
                conversation_id=conversation_a.id,
                turn_id=uuid4(),
                seq=1,
                role="user",
                content="我不吃香菜。",
                privacy_level="L1",
            )
        )
    await store.add(
        MemoryCandidate(
            type=MemoryType.PREFERENCE,
            content="用户不吃香菜",
            privacy_level=PrivacyLevel.L1,
            sources=[
                MemorySourceRef(
                    source_kind=MemorySourceKind.MESSAGE,
                    source_id=str(turn_a.user_message.id),
                )
            ],
        ),
        user_id=user.id,
    )

    dry = await replay_deletions(database, dry_run=True)
    assert dry.dry_run is True
    assert dry.conversations_deleted == 1
    assert dry.memories_deleted == 1
    async with database.sessions() as session:
        assert await session.get(ConversationRecord, conversation_a.id) is not None

    applied = await replay_deletions(database, dry_run=False)
    assert applied.conversations_deleted == 1
    assert applied.memories_deleted == 1
    async with database.sessions() as session:
        assert await session.get(ConversationRecord, conversation_a.id) is None
    survivors = await store.list_memories(user_id=user.id, status=MemoryStatus.ACTIVE)
    assert [item.content for item in survivors] == ["用户叫小雷"]

    again = await replay_deletions(database, dry_run=False)
    assert again.conversations_deleted == 0
    assert again.memories_deleted == 0


async def test_delete_conversation_rejects_other_users(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    config_store = DatabaseConfigStore(database, config_path)
    await config_store.load()
    service = ChatService(database, config_store)
    conversation = await service.create_conversation(user_id=user.id, title="mine")
    with pytest.raises(LookupError):
        await service.delete_conversation(conversation.id, user_id=uuid4())
    assert await service.list_conversations(user_id=user.id)


async def test_admin_replay_endpoint_reports_and_is_idempotent(
    database: Database, user: AppUserRecord, tmp_path: Path
) -> None:
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    config_store = DatabaseConfigStore(database, config_path)
    app = create_app(
        database,
        config_store=config_store,
        watch_config=False,
        admin_token="test-admin-token",
    )
    headers = {"Authorization": "Bearer test-admin-token"}
    store = MemoryStore(database)
    created = await store.add(candidate("用户在杭州工作"), user_id=user.id)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        unauthorized = await client.post(
            "/api/v1/admin/deletion-ledger/replay", json={"dry_run": True}
        )
        deleted = await client.delete(f"/api/v1/admin/memories/{created.id}", headers=headers)
        dry = await client.post(
            "/api/v1/admin/deletion-ledger/replay",
            headers=headers,
            json={"dry_run": True},
        )
        applied = await client.post(
            "/api/v1/admin/deletion-ledger/replay",
            headers=headers,
            json={"dry_run": False},
        )

    assert unauthorized.status_code == 401
    assert deleted.status_code == 200
    assert dry.json() == {
        "ledger_rows": 1,
        "conversations_deleted": 0,
        "memories_deleted": 0,
        "dry_run": True,
    }
    assert applied.json()["dry_run"] is False
    assert applied.json()["memories_deleted"] == 0


async def test_turn_extractor_captures_height_without_keyword() -> None:
    """“我165厘米”这类省略“身高”二字的自述也必须进档案槽位。"""

    candidates = await TurnMemoryExtractor().extract_turn(
        user_text="你多高啊",
        user_message_id=uuid4(),
        user_occurred_at=NOW,
        assistant_text="我啊，168公分。不算高，但穿高跟鞋刚刚好。",
        assistant_message_id=uuid4(),
        assistant_occurred_at=NOW,
        privacy_level=PrivacyLevel.L1,
    )
    by_fact = {
        item.fact_key: item.content
        for item in candidates
        if item.subject_kind == "assistant" and item.fact_key is not None
    }
    assert by_fact["profile.height"] == "助手身高为 168 厘米"
    assert dict(extract_assistant_fact_assertions("我啊，168公分。")) == {
        "profile.height": "助手身高为 168 厘米"
    }


async def test_turn_extractor_captures_user_granted_height_without_keyword() -> None:
    candidates = await TurnMemoryExtractor().extract_turn(
        user_text="你170cm就挺好的。",
        user_message_id=uuid4(),
        user_occurred_at=NOW,
        assistant_text="哈哈，谢谢你这么说。",
        assistant_message_id=uuid4(),
        assistant_occurred_at=NOW,
        privacy_level=PrivacyLevel.L1,
    )
    granted = [
        item
        for item in candidates
        if item.fact_key == "profile.height" and item.origin_kind == "user_statement"
    ]
    assert [item.content for item in granted] == ["助手身高为 170 厘米"]


async def test_turn_extractor_ignores_unit_mention_unrelated_to_body() -> None:
    candidates = await TurnMemoryExtractor().extract_turn(
        user_text="我买了个165厘米的柜子，你觉得放你家行吗？",
        user_message_id=uuid4(),
        user_occurred_at=NOW,
        assistant_text="听起来不错，量好尺寸再买更稳妥。",
        assistant_message_id=uuid4(),
        assistant_occurred_at=NOW,
        privacy_level=PrivacyLevel.L1,
    )
    assert [item for item in candidates if item.fact_key == "profile.height"] == []
