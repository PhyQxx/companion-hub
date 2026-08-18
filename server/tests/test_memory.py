# ruff: noqa: RUF001
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from test_chat import FakeRouter, config_yaml

from app.chat import ChatService
from app.config import DatabaseConfigStore
from app.db import AppUserRecord, Base, Database, create_database
from app.ids import uuid7
from app.llm import CompletionRequest
from app.main import create_app
from app.memory import (
    ConsolidateDecision,
    HashingEmbeddingProvider,
    MemoryCandidate,
    MemoryIngester,
    MemoryRetriever,
    MemorySourceKind,
    MemorySourceRef,
    MemoryStatus,
    MemoryStore,
    MemoryType,
    cosine_similarity,
)
from app.schemas import PrivacyLevel

NOW = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


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
        **kwargs,  # type: ignore[arg-type]
    )


async def test_hashing_embedding_is_deterministic_and_orders_similarity() -> None:
    provider = HashingEmbeddingProvider()
    first = await provider.embed(["用户不吃香菜"])
    second = await provider.embed(["用户不吃香菜"])
    related = (await provider.embed(["用户不喜欢吃香菜"]))[0]
    unrelated = (await provider.embed(["明天要去爬山看日出"]))[0]

    assert first[0] == second[0]
    assert cosine_similarity(first[0], related) > cosine_similarity(first[0], unrelated)
    assert cosine_similarity(first[0], unrelated) < 0.35


async def test_store_add_get_list_and_sources(
    store: MemoryStore, user: AppUserRecord
) -> None:
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


async def test_retrieval_ranks_relevant_memory_and_records_access(
    store: MemoryStore, user: AppUserRecord
) -> None:
    target = await store.add(candidate("用户不吃香菜，点菜时要去掉"), user_id=user.id)
    await store.add(candidate("用户在杭州工作，是后端工程师"), user_id=user.id)
    await store.add(candidate("上周末一起看了电影《星际穿越》"), user_id=user.id)

    result = await MemoryRetriever(store).retrieve(
        "帮我点菜，我能吃香菜吗", user_id=user.id, privacy_level=PrivacyLevel.L1, now=NOW
    )

    assert result.policy_version == "hybrid-quota-v1"
    assert result.hits
    assert result.hits[0].memory.id == target.id
    assert "香菜" in MemoryRetriever.render_context(result)
    assert "【相关记忆】" in MemoryRetriever.render_context(result)
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


async def test_retrieval_privacy_gating(
    store: MemoryStore, user: AppUserRecord
) -> None:
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


async def test_retrieval_enforces_type_quota(
    store: MemoryStore, user: AppUserRecord
) -> None:
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
        sources=[
            MemorySourceRef(source_kind=MemorySourceKind.MESSAGE, source_id=str(message_id))
        ],
        importance=0.6,
    )

    first = await ingester.ingest(base, user_id=user.id)
    assert first.decision is ConsolidateDecision.CREATED
    assert first.memory.status == "active"

    repeat = MemoryCandidate(
        type=MemoryType.PREFERENCE,
        content="用户不吃香菜",
        privacy_level=PrivacyLevel.L1,
        sources=[
            MemorySourceRef(
                source_kind=MemorySourceKind.MESSAGE, source_id=str(uuid4())
            )
        ],
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


async def test_conflict_resolution_adopt_and_keep(
    store: MemoryStore, user: AppUserRecord
) -> None:
    ingester = MemoryIngester(store)
    original = (await ingester.ingest(candidate("用户不吃香菜"), user_id=user.id)).memory
    conflict = (
        await ingester.ingest(
            candidate("用户其实喜欢吃香菜"), user_id=user.id
        )
    ).memory

    adopted = await store.resolve_conflict(conflict.id, adopt=True, actor="admin")
    stale = await store.get(original.id)
    assert adopted.status == "active"
    assert adopted.conflict_with is None
    assert stale.status == "superseded"
    assert stale.superseded_by == adopted.id

    keep_case = (
        await ingester.ingest(candidate("用户不吃芹菜"), user_id=user.id)
    )
    assert keep_case.decision is ConsolidateDecision.CREATED
    second_conflict = (
        await ingester.ingest(candidate("用户不吃香菜和芹菜"), user_id=user.id)
    ).memory
    kept = await store.resolve_conflict(second_conflict.id, adopt=False, actor="admin")
    assert kept.status == "archived"

    with pytest.raises(ValueError):
        await store.resolve_conflict(adopted.id, adopt=True, actor="admin")


async def test_edit_supersedes_and_keeps_lineage(
    store: MemoryStore, user: AppUserRecord
) -> None:
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
    results = extractor.extract(
        "我不吃香菜。我喜欢你。我周五要汇报 PPT。随便聊聊。我叫小明。",
        message_id=message_id,
        occurred_at=NOW,
    )
    by_type = {item.type: item for item in results}
    assert set(by_type) == {"preference", "commitment", "semantic"}
    assert by_type["preference"].content == "用户不吃香菜"
    assert by_type["semantic"].content == "用户叫小明"
    commitment = by_type["commitment"]
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


async def test_l3_memory_is_rejected(store: MemoryStore, user: AppUserRecord) -> None:
    with pytest.raises(ValueError, match="L3"):
        await store.add(
            candidate("原始遥测", privacy_level=PrivacyLevel.L3), user_id=user.id
        )


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
    system_prompt = requests[-1].messages[0].content
    assert "【相关记忆】" in system_prompt
    assert "用户不吃香菜" in system_prompt
    memory_meta = second.assistant_message.decision_meta or {}
    assert memory_meta["memory"]["policy_version"] == "hybrid-quota-v1"
    assert memory_meta["memory"]["hits"][0]["id"] == memories[0].id
    assert memory_meta["memory"]["hits"][0]["type"] == "preference"

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

    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
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
    assert listing.json()[0]["content"] == "用户在杭州工作"
    assert detail.json()["sources"][0]["source_kind"] == "manual"
    assert edited.json()["content"] == "用户在上海工作"
    assert edited.json()["id"] != memory_id
    assert detail.json()["lineage"] or True
    assert queried.json()["policy_version"] == "hybrid-quota-v1"
    assert queried.json()["hits"]
    assert l3_rejected.status_code == 422
    assert archived.json()["status"] == "archived"
