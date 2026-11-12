from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm.attributes import flag_modified
from test_goal_delivery_runs import seed
from test_memory import candidate
from test_run_cancel_fence import prepared

from app.db import Database, MemoryRecord
from app.memory import MemoryStore
from app.memory.lifecycle import LifecycleConflict, MemoryLifecycle
from app.memory.lifecycle_policy import MemoryTier, evaluate
from app.memory.persistence import MemoryLifecycleRecord, MemoryUsageRecord


@pytest.fixture(params=["sqlite", "postgresql"])
async def database(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Database]:
    storage = await prepared(request.param, tmp_path)
    try:
        yield storage.database
    finally:
        await storage.close()


NOW = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "tier,days,target",
    [
        ("core", 180, "important"),
        ("important", 90, "medium"),
        ("medium", 30, "ordinary"),
        ("ordinary", 60, "low"),
        ("low", 180, "dormant"),
    ],
)
@pytest.mark.parametrize("importance,multiplier", [(0.2, 1), (0.5, 2), (0.9, 4)])
def test_decay_boundaries(
    tier: str, days: int, target: str, importance: float, multiplier: int
) -> None:
    start = NOW - timedelta(days=days * multiplier)
    args: dict[str, Any] = dict(
        tier=MemoryTier(tier),
        changed_at=start,
        created_at=start,
        last_used_at=None,
        importance=importance,
        locked=False,
    )
    assert evaluate(**args, now=NOW - timedelta(microseconds=1)).tier == tier
    assert evaluate(**args, now=NOW).tier == target
    assert evaluate(**{**args, "locked": True}, now=NOW).tier == tier


async def test_usage_deduplication_promotion_and_revision(database: Database) -> None:
    owner, _, _ = await seed(database)
    store, lifecycle = MemoryStore(database), MemoryLifecycle(database)
    entry = await store.add(candidate("fixture hobby"), user_id=owner)
    moment = entry.created_at + timedelta(seconds=1)
    for day in range(3):
        for _ in range(2):
            await lifecycle.use(
                entry.id,
                user_id=owner,
                turn_key=f"turn-{day}",
                kind="user_reaffirmed",
                now=moment + timedelta(days=day),
            )
    current = await store.get(entry.id)
    assert current.tier == "important" and current.use_count == 3
    assert current.updated_at == entry.updated_at
    async with database.sessions() as s:
        events = list(await s.scalars(select(MemoryUsageRecord)))
        assert len(events) == 3
        assert len(list(await s.scalars(select(MemoryLifecycleRecord)))) == 1
    with pytest.raises(LifecycleConflict):
        await lifecycle.set_tier(
            entry.id, user_id=owner, tier=MemoryTier.CORE, locked=True, expected_version=0
        )
    await lifecycle.set_tier(
        entry.id,
        user_id=owner,
        tier=MemoryTier.DORMANT,
        locked=False,
        expected_version=current.lifecycle_version,
    )
    await lifecycle.use(
        entry.id,
        user_id=owner,
        turn_key="wake",
        kind="used_in_answer",
        now=moment + timedelta(days=4),
    )
    assert (await store.get(entry.id)).tier == "medium"


async def test_daily_spam_does_not_promote_and_foreign_owner_rejected(database: Database) -> None:
    owner, _, _ = await seed(database)
    foreign, _, _ = await seed(database)
    store, lifecycle = MemoryStore(database), MemoryLifecycle(database)
    entry = await store.add(candidate("fixture"), user_id=owner)
    for number in range(12):
        await lifecycle.use(entry.id, user_id=owner, turn_key=str(number), kind="user_confirmed")
    assert (await store.get(entry.id)).tier == "medium"
    with pytest.raises(LookupError):
        await lifecycle.use(entry.id, user_id=foreign, turn_key="foreign", kind="user_confirmed")


async def test_decay_is_one_step_and_idempotent(database: Database) -> None:
    owner, _, _ = await seed(database)
    store, lifecycle = MemoryStore(database), MemoryLifecycle(database)
    entry = await store.add(candidate("fixture"), user_id=owner)
    async with database.sessions.begin() as s:
        row = await s.get_one(MemoryRecord, entry.id)
        row.created_at = row.tier_changed_at = NOW - timedelta(days=1000)
        row.updated_at = entry.updated_at
        flag_modified(row, "updated_at")
    assert await lifecycle.decay_batch(now=NOW) == 1
    assert await lifecycle.decay_batch(now=NOW) == 0
    assert (await store.get(entry.id)).tier == "ordinary"
    assert (await store.get(entry.id)).updated_at == entry.updated_at


async def test_cold_exact_retrieval_and_deleted_usage_cleanup(database: Database) -> None:
    from app.memory import MemoryRetriever
    from app.memory.extraction_rules import _infer_user_fact_key
    from app.schemas import PrivacyLevel

    owner, _, _ = await seed(database)
    store, lifecycle = MemoryStore(database), MemoryLifecycle(database)
    content = "用户的女儿小晴生日是2024年5月6日"
    key = _infer_user_fact_key(content)
    assert key == "person.小晴.birthday"
    entry = await store.add(candidate(content, fact_key=key), user_id=owner)
    await lifecycle.set_tier(
        entry.id, user_id=owner, tier=MemoryTier.DORMANT, locked=False, expected_version=0
    )
    hits = await MemoryRetriever(store).retrieve(
        "小晴的生日是哪天", user_id=owner, privacy_level=PrivacyLevel.L1
    )
    assert hits.hits[0].memory.id == entry.id
    assert "exact_fact" in hits.hits[0].reasons
    assert (await store.get(entry.id)).use_count == 0
    await lifecycle.use(entry.id, user_id=owner, turn_key="confirmed", kind="user_confirmed")
    await store.hard_delete(entry.id, actor="fixture", reason="delete")
    async with database.sessions() as session:
        assert not list(await session.scalars(select(MemoryUsageRecord)))
        assert not list(await session.scalars(select(MemoryLifecycleRecord)))


async def test_chat_saves_multiple_facts_and_receipt(database: Database, tmp_path: Path) -> None:
    import json

    from test_memory import ScriptedRouter, _chat_service

    from app.memory import LlmMemoryExtractor
    from app.schemas import PrivacyLevel

    owner, _, _ = await seed(database)
    facts = [
        "用户喜欢晨跑",
        "用户女儿小晴生日是2024年5月6日",
        "用户住在青岛",
        "用户养了一只猫",
        "用户周末学习钢琴",
        "用户不吃香菜",
    ]
    router = ScriptedRouter(
        [
            "谢谢你介绍这些生活习惯。",
            json.dumps(
                {
                    "candidates": [
                        {"type": "semantic", "content": fact, "confidence": 0.9} for fact in facts
                    ]
                },
                ensure_ascii=False,
            ),
        ]
    )
    service = await _chat_service(database, tmp_path, router, extractor=LlmMemoryExtractor())
    conversation = await service.create_conversation(user_id=owner, title="synthetic lifecycle")
    await service.send_message(
        conversation.id, user_id=owner, text="。".join(facts), privacy_level=PrivacyLevel.L1
    )
    await service.drain_background_work()
    entries = await MemoryStore(database).list_memories(user_id=owner)
    assert len(entries) == 6
    assert all(entry.tier == "medium" for entry in entries)
    messages = await service.list_messages(conversation.id, user_id=owner)
    receipt = cast(dict[str, Any], (messages[-1].decision_meta or {})["memory_receipt"])
    assert receipt["state"] == "completed" and receipt["created"] == 6


async def test_old_consumer_cannot_take_new_protocol_jobs(database: Database) -> None:
    from app.chat.postcommit import RESOURCE
    from app.jobs import JobEngine

    engine = JobEngine(database)
    job = await engine.submit("chat.memory", {}, resource_class=RESOURCE)
    assert await engine.claim("old", resource_class="chat-postcommit") is None
    claimed = await engine.claim("new", resource_class=RESOURCE)
    assert claimed is not None and claimed.id == job.id


async def test_concurrent_same_turn_only_counts_once(database: Database) -> None:
    import asyncio

    owner, _, _ = await seed(database)
    store = MemoryStore(database)
    entry = await store.add(candidate("synthetic concurrency fact"), user_id=owner)
    await asyncio.gather(
        *[
            store.record_use(entry.id, user_id=owner, turn_key="same", kind="used_in_action")
            for _ in range(4)
        ]
    )
    assert (await store.get(entry.id)).use_count == 1


async def test_switch_off_preserves_state(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _, _ = await seed(database)
    store = MemoryStore(database)
    entry = await store.add(candidate("synthetic rollout fact"), user_id=owner)
    monkeypatch.setenv("ARIA_MEMORY_COLLECT_USAGE", "0")
    monkeypatch.setenv("ARIA_MEMORY_AUTO_DECAY", "0")
    await store.record_use(entry.id, user_id=owner, turn_key="disabled", kind="user_confirmed")
    assert await store.decay_memories() == 0
    restored = await store.get(entry.id)
    assert restored.use_count == 0 and restored.tier == "medium"


async def test_admin_lifecycle_version_history_and_confirmation(
    database: Database, tmp_path: Path
) -> None:
    from httpx import ASGITransport, AsyncClient
    from test_chat import config_yaml

    from app.config import DatabaseConfigStore
    from app.main import create_app

    owner, _, _ = await seed(database)
    entry = await MemoryStore(database).add(candidate("synthetic admin fact"), user_id=owner)
    path = tmp_path / "hub.yaml"
    path.write_text(config_yaml())
    app = create_app(
        database,
        config_store=DatabaseConfigStore(database, path),
        watch_config=False,
        admin_token="fixture-token",
    )
    headers = {"Authorization": "Bearer fixture-token"}
    prefix = f"/api/v1/admin/memories/{entry.id}"
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get(prefix + "/lifecycle-events")).status_code == 401
        payload = {"tier": "core", "locked": True, "expected_version": 0}
        changed = await client.patch(prefix + "/lifecycle", json=payload, headers=headers)
        assert changed.status_code == 200 and changed.json()["tier_locked"]
        assert (
            await client.patch(prefix + "/lifecycle", json=payload, headers=headers)
        ).status_code == 409
        events = (await client.get(prefix + "/lifecycle-events", headers=headers)).json()
        assert events[0]["reason"] == "manual_locked"
        for _ in range(2):
            confirmed = await client.post(prefix + "/confirm", headers=headers)
            assert confirmed.status_code == 200 and confirmed.json()["use_count"] == 1
        assert confirmed.json()["updated_at"] == changed.json()["updated_at"]


async def test_corrected_version_preserves_explicit_lock(database: Database) -> None:
    owner, _, _ = await seed(database)
    store = MemoryStore(database)
    entry = await store.add(candidate("synthetic old fact"), user_id=owner)
    await MemoryLifecycle(database).set_tier(
        entry.id, user_id=owner, tier=MemoryTier.CORE, locked=True, expected_version=0
    )
    replacement = await store.edit(
        entry.id,
        user_id=owner,
        content="synthetic corrected fact",
        actor="fixture",
        reason="fixture correction",
    )
    assert replacement.tier == "medium" and replacement.tier_locked


async def test_shadow_retrieval_keeps_relevance_above_strength(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.memory import MemoryRetriever
    from app.schemas.common import PrivacyLevel

    owner, _, _ = await seed(database)
    store, lifecycle = MemoryStore(database), MemoryLifecycle(database)
    relevant = await store.add(candidate("用户不吃香菜，点菜时需要去掉香菜"), user_id=owner)
    irrelevant = await store.add(candidate("用户喜欢研究合成航天器历史"), user_id=owner)
    await lifecycle.set_tier(
        relevant.id, user_id=owner, tier=MemoryTier.DORMANT, locked=False, expected_version=0
    )
    await lifecycle.set_tier(
        irrelevant.id, user_id=owner, tier=MemoryTier.CORE, locked=False, expected_version=0
    )
    for enabled in ("0", "1"):
        monkeypatch.setenv("ARIA_MEMORY_TIERED_RETRIEVAL", enabled)
        for deep in (False, True):
            result = await MemoryRetriever(store).retrieve(
                "点菜时能吃香菜吗", user_id=owner, privacy_level=PrivacyLevel.L1, deep=deep
            )
            assert result.hits and result.hits[0].memory.id == relevant.id
            assert irrelevant.id not in {hit.memory.id for hit in result.hits}
    assert (await store.get(relevant.id)).use_count == 0


async def test_dedup_support_finds_low_importance_duplicate(database: Database) -> None:
    """守门（方案 A1）：查重窗口不按重要性截断。

    600 条高重要性噪声 + 1 条低重要性目标记忆；与目标几乎相同的新候选
    必须判 supported——旧实现在"重要性前 500"窗口内找不到目标，会误判 created。
    """
    from app.memory import ConsolidateDecision, MemoryIngester

    owner, _, _ = await seed(database)
    store = MemoryStore(database)
    target = "用户有一条冷门的小众爱好：收集各地老火车票"
    await store.add(candidate(target, importance=0.1), user_id=owner)
    for index in range(600):
        await store.add(
            candidate(f"fixture noise memory number {index:04d}", importance=0.9),
            user_id=owner,
        )
    outcome = await MemoryIngester(store).ingest(
        candidate(f"{target}。", importance=0.9), user_id=owner
    )
    assert outcome.decision is ConsolidateDecision.SUPPORTED


async def test_ai_initial_tier_gates_and_audit(database: Database) -> None:
    from app.memory import MemoryStatus

    owner, _, _ = await seed(database)
    store = MemoryStore(database)
    core = await store.add(
        candidate(
            "用户的女儿小晴生日是2024年5月6日",
            importance=0.9,
            confidence=0.9,
            initial_tier="core",
        ),
        user_id=owner,
    )
    important = await store.add(
        candidate(
            "fixture important habit",
            importance=0.9,
            confidence=0.7,
            initial_tier="important",
        ),
        user_id=owner,
    )
    gated = await store.add(
        candidate(
            "fixture weak core claim",
            importance=0.9,
            confidence=0.5,
            initial_tier="core",
        ),
        user_id=owner,
    )
    gated_important = await store.add(
        candidate(
            "fixture weak important claim",
            importance=0.9,
            confidence=0.5,
            initial_tier="important",
        ),
        user_id=owner,
    )
    missing_confidence = await store.add(
        candidate("fixture missing confidence", importance=0.9, initial_tier="core"),
        user_id=owner,
    )
    below_floor = await store.add(
        candidate(
            "fixture below floor claim",
            importance=0.9,
            confidence=0.9,
            initial_tier="ordinary",
        ),
        user_id=owner,
    )
    invalid = await store.add(
        candidate(
            "fixture invalid tier claim",
            importance=0.9,
            confidence=0.9,
            initial_tier="spam",
        ),
        user_id=owner,
    )
    assert core.tier == "core" and important.tier == "important"
    assert gated.tier == "medium"
    assert gated_important.tier == "medium"
    assert missing_confidence.tier == "medium"
    assert below_floor.tier == "medium" and invalid.tier == "medium"
    async with database.sessions() as session:
        events = list(await session.scalars(select(MemoryLifecycleRecord)))
    trail = [(event.memory_id, event.old_tier, event.new_tier, event.reason) for event in events]
    assert trail == [
        (core.id, "medium", "core", "ai_initial"),
        (important.id, "medium", "important", "ai_initial"),
    ]
    # 待决冲突记忆不得带初始等级入库，采纳前保持中级。
    conflict = await store.add(
        candidate(
            "fixture conflict core claim",
            importance=0.9,
            confidence=0.9,
            initial_tier="core",
        ),
        user_id=owner,
        status=MemoryStatus.CONFLICT,
    )
    assert conflict.tier == "medium"


async def test_ai_initial_tier_switch_off_falls_back_to_medium(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _, _ = await seed(database)
    store = MemoryStore(database)
    monkeypatch.setenv("ARIA_MEMORY_AI_INITIAL_TIER", "0")
    entry = await store.add(
        candidate(
            "fixture rolled back tier claim",
            importance=0.9,
            confidence=0.9,
            initial_tier="core",
        ),
        user_id=owner,
    )
    assert entry.tier == "medium"
    async with database.sessions() as session:
        assert not list(await session.scalars(select(MemoryLifecycleRecord)))


async def test_chat_saves_family_facts_at_ai_proposed_tier(
    database: Database, tmp_path: Path
) -> None:
    import json

    from test_memory import ScriptedRouter, _chat_service

    from app.memory import LlmMemoryExtractor
    from app.schemas import PrivacyLevel

    owner, _, _ = await seed(database)
    payload = [
        {
            "type": "semantic",
            "content": "用户女儿小晴生日是2024年5月6日",
            "importance": 0.9,
            "confidence": 0.9,
            "tier": "core",
        },
        {
            "type": "semantic",
            "content": "用户住在青岛",
            "importance": 0.9,
            "confidence": 0.9,
            "tier": "core",
        },
        {"type": "semantic", "content": "用户养了一只猫", "importance": 0.5, "confidence": 0.9},
    ]
    router = ScriptedRouter(
        ["谢谢你介绍家人。", json.dumps({"candidates": payload}, ensure_ascii=False)]
    )
    service = await _chat_service(database, tmp_path, router, extractor=LlmMemoryExtractor())
    conversation = await service.create_conversation(user_id=owner, title="synthetic ai tier")
    await service.send_message(
        conversation.id,
        user_id=owner,
        text="我女儿小晴生日是2024年5月6日。我住在青岛。我养了一只猫。",
        privacy_level=PrivacyLevel.L1,
    )
    await service.drain_background_work()
    entries = await MemoryStore(database).list_memories(user_id=owner)
    tiers = {entry.content: entry.tier for entry in entries}
    assert tiers["用户女儿小晴生日是2024年5月6日"] == "core"
    assert tiers["用户住在青岛"] == "core"
    assert tiers["用户养了一只猫"] == "medium"
    async with database.sessions() as session:
        events = list(await session.scalars(select(MemoryLifecycleRecord)))
    assert {event.reason for event in events} == {"ai_initial"}
    assert {event.new_tier for event in events} == {"core"}


async def test_ai_gate_env_threshold_and_rejection_logging(
    database: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """B3：AI 定级门禁阈值可经环境校准；被拒建议留结构化日志。"""
    owner, _, _ = await seed(database)
    store = MemoryStore(database)
    gate = {"importance": 0.9, "confidence": 0.85, "initial_tier": "core"}

    monkeypatch.setenv("ARIA_MEMORY_AI_TIER_CORE_GATE", "0.9")
    with caplog.at_level("INFO", logger="app.memory.store"):
        stricter = await store.add(candidate("fixture stricter gate fact", **gate), user_id=owner)
    assert stricter.tier == "medium"  # 0.85 < 0.9 被门禁降级
    assert any(
        "memory.initial_tier_gate" in record.message and "suggested=core" in record.message
        for record in caplog.records
    )

    monkeypatch.setenv("ARIA_MEMORY_AI_TIER_CORE_GATE", "0.8")
    accepted = await store.add(candidate("fixture relaxed gate fact", **gate), user_id=owner)
    assert accepted.tier == "core"

    monkeypatch.setenv("ARIA_MEMORY_AI_TIER_CORE_GATE", "not-a-number")
    fallback = await store.add(candidate("fixture fallback gate fact", **gate), user_id=owner)
    assert fallback.tier == "core"  # 非法阈值回落默认 0.8


async def test_admin_tier_stats_endpoint(database: Database, tmp_path: Path) -> None:
    """B3：tier-stats 聚合等级分布与初始定级审计。"""
    from httpx import ASGITransport, AsyncClient
    from test_chat import config_yaml

    from app.config import DatabaseConfigStore
    from app.main import create_app

    owner, _, _ = await seed(database)
    store = MemoryStore(database)
    await store.add(
        candidate(
            "fixture stats core fact",
            importance=0.9,
            confidence=0.9,
            initial_tier="core",
        ),
        user_id=owner,
    )
    await store.add(candidate("fixture stats medium fact"), user_id=owner)
    path = tmp_path / "hub.yaml"
    path.write_text(config_yaml())
    app = create_app(
        database,
        config_store=DatabaseConfigStore(database, path),
        watch_config=False,
        admin_token="fixture-token",
    )
    headers = {"Authorization": "Bearer fixture-token"}
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/api/v1/admin/memories/tier-stats")).status_code == 401
        payload = (await client.get("/api/v1/admin/memories/tier-stats", headers=headers)).json()
    assert payload["active_by_tier"].get("core") == 1
    assert payload["active_by_tier"].get("medium") == 1
    assert payload["initial_tier"]["total"]["ai_initial"]["core"] == 1
    assert payload["initial_tier"]["recent_7d"]["ai_initial"]["core"] == 1


async def test_periodic_wake_scheduling_and_wake_batch(database: Database) -> None:
    """B4：生日/周年事实排程临近唤醒；到期低等级升回中级并推进下一年。"""
    from datetime import UTC as _UTC

    owner, _, _ = await seed(database)
    store, lifecycle = MemoryStore(database), MemoryLifecycle(database)

    birthday = await store.add(
        candidate("用户的女儿小晴生日是2024年5月6日", fact_key="person.小晴.birthday"),
        user_id=owner,
    )
    anniversary = await store.add(candidate("用户和伴侣的结婚纪念日是每年6月18日"), user_id=owner)
    plain = await store.add(candidate("fixture non periodic fact"), user_id=owner)

    from app.memory.lifecycle_policy import periodic_wake_at as _wake

    # 排程锚定真实入库时间：期望按同口径独立计算（下一个周年 - 7 天）。
    assert birthday.next_relevant_at == _wake(
        "用户的女儿小晴生日是2024年5月6日",
        "person.小晴.birthday",
        now=birthday.created_at,
    )
    assert anniversary.next_relevant_at == _wake(
        "用户和伴侣的结婚纪念日是每年6月18日", None, now=anniversary.created_at
    )
    assert birthday.next_relevant_at is not None and anniversary.next_relevant_at is not None
    assert plain.next_relevant_at is None

    # 全部压到低等级并制造到期：唤醒只提升可见性，core 不动。
    for entry, tier in ((birthday, MemoryTier.DORMANT), (anniversary, MemoryTier.LOW)):
        await lifecycle.set_tier(
            entry.id, user_id=owner, tier=tier, locked=False, expected_version=0
        )
    core_fact = await store.add(
        candidate(
            "fixture core anniversary 每年6月18日也是重要日子",
            importance=0.9,
            confidence=0.9,
            initial_tier="core",
        ),
        user_id=owner,
    )
    async with database.sessions.begin() as session:
        from app.db import MemoryRecord as _MemoryRecord

        await session.execute(
            update(_MemoryRecord)
            .where(_MemoryRecord.id.in_([birthday.id, anniversary.id, core_fact.id]))
            .values(next_relevant_at=datetime(2030, 6, 2, tzinfo=_UTC))
        )
    woken = await lifecycle.wake_due_batch(now=datetime(2030, 6, 3, tzinfo=UTC))
    assert woken == 2
    woke_birthday = await store.get(birthday.id)
    woke_anniversary = await store.get(anniversary.id)
    core_row = await store.get(core_fact.id)
    assert woke_birthday.tier == "medium" and woke_anniversary.tier == "medium"
    assert core_row.tier == "core"  # 中级及以上不重复唤醒
    # 唤醒后推进：生日窗口已过（5月6日 < 6月3日）→ 下一周年 2031；
    # 纪念日周年（6月18日）尚未到 → 保留当年窗口 2030-06-11（周年前
    # tier 已是中级不会重复触发；周年过完若再降级，重扫时自愈推进下一年）。
    assert woke_birthday.next_relevant_at == datetime(2031, 4, 29, tzinfo=UTC)
    assert woke_anniversary.next_relevant_at == datetime(2030, 6, 11, tzinfo=UTC)
    assert await lifecycle.wake_due_batch(now=datetime(2030, 6, 3, tzinfo=UTC)) == 0
    async with database.sessions() as session:
        events = list(await session.scalars(select(MemoryLifecycleRecord)))
    assert {event.reason for event in events} >= {"periodic_wake"}
    wake_events = [event for event in events if event.reason == "periodic_wake"]
    assert {(event.old_tier, event.new_tier) for event in wake_events} == {
        ("dormant", "medium"),
        ("low", "medium"),
    }
    # 唤醒不计使用分
    assert woke_birthday.use_count == 0


async def test_periodic_wake_switch_off(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _, _ = await seed(database)
    store = MemoryStore(database)
    entry = await store.add(
        candidate("用户的儿子乐乐生日是2020年3月12日", fact_key="person.乐乐.birthday"),
        user_id=owner,
    )
    await MemoryLifecycle(database).set_tier(
        entry.id, user_id=owner, tier=MemoryTier.DORMANT, locked=False, expected_version=0
    )
    monkeypatch.setenv("ARIA_MEMORY_PERIODIC_WAKE", "0")
    assert await store.wake_periodic_memories() == 0
    assert (await store.get(entry.id)).tier == "dormant"


async def test_quality_stats_counts_backlog_and_wakes(database: Database) -> None:
    """C3/C6：降级积压、待唤醒与唤醒事件计数。"""
    owner, _, _ = await seed(database)
    store, lifecycle = MemoryStore(database), MemoryLifecycle(database)
    counts = await lifecycle.quality_counts()
    assert counts["decay_backlog"] == 0 and counts["pending_wakes"] == 0
    plain = await store.add(candidate("fixture backlog fact"), user_id=owner)
    del plain
    # 新记忆未排 decay（next_decay_at NULL）且未锁定 → 计入积压口径
    assert (await lifecycle.quality_counts())["decay_backlog"] == 1
    birthday = await store.add(
        candidate("用户的儿子乐乐生日是2020年3月12日", fact_key="person.乐乐.birthday"),
        user_id=owner,
    )
    await lifecycle.set_tier(
        birthday.id, user_id=owner, tier=MemoryTier.DORMANT, locked=False, expected_version=0
    )
    async with database.sessions.begin() as session:
        from app.db import MemoryRecord as _MemoryRecord

        await session.execute(
            update(_MemoryRecord)
            .where(_MemoryRecord.id == birthday.id)
            .values(next_relevant_at=datetime.now(UTC) - timedelta(days=1))
        )
    refreshed = await lifecycle.quality_counts()
    assert refreshed["pending_wakes"] == 1
    assert await lifecycle.wake_due_batch() == 1
    final = await lifecycle.quality_counts()
    assert final["pending_wakes"] == 0
    assert final["periodic_wake_events"] == 1
    assert final["periodic_wake_events_7d"] == 1


async def test_admin_quality_stats_endpoint(database: Database, tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient
    from test_chat import config_yaml

    from app.config import DatabaseConfigStore
    from app.main import create_app

    await seed(database)
    path = tmp_path / "hub.yaml"
    path.write_text(config_yaml())
    app = create_app(
        database,
        config_store=DatabaseConfigStore(database, path),
        watch_config=False,
        admin_token="fixture-token",
    )
    headers = {"Authorization": "Bearer fixture-token"}
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/api/v1/admin/memories/quality-stats")).status_code == 401
        payload = (await client.get("/api/v1/admin/memories/quality-stats", headers=headers)).json()
    assert set(payload) == {
        "decay_backlog",
        "pending_wakes",
        "periodic_wake_events",
        "periodic_wake_events_7d",
    }
    assert payload["decay_backlog"] == 0  # 本用例未建记忆


async def test_chat_chunk_cursor_persists_steps_and_gate_off_falls_back(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """方案 C1/02A：超长消息提取逐块记 memory-chunk 游标步骤；开关回退整消息。"""
    import json

    from sqlalchemy import select as _select
    from test_memory import ScriptedRouter, _chat_service

    from app.db import JobRecord, JobStepRecord
    from app.memory import LlmMemoryExtractor
    from app.memory.extraction_rules import MAX_EXTRACT_INPUT_CHARS
    from app.memory.persistence import MemoryRecord as _MemoryRecord
    from app.schemas import PrivacyLevel

    owner, _, _ = await seed(database)
    fact = "用户女儿小晴生日是2024年5月6日。"
    long_text = fact * 800  # 12800 字符，远超单块上限：4 个顶层块（0/3800/7600/11400）
    assert len(long_text) > MAX_EXTRACT_INPUT_CHARS
    chunk_payload = json.dumps(
        {
            "candidates": [
                {
                    "type": "semantic",
                    "content": "用户女儿小晴生日是2024年5月6日",
                    "importance": 0.9,
                    "confidence": 0.9,
                    "tier": "core",
                }
            ]
        },
        ensure_ascii=False,
    )
    router = ScriptedRouter(
        ["谢谢你介绍家人。", chunk_payload, chunk_payload, chunk_payload, chunk_payload]
    )
    service = await _chat_service(database, tmp_path, router, extractor=LlmMemoryExtractor())
    conversation = await service.create_conversation(user_id=owner, title="synthetic chunk")
    await service.send_message(
        conversation.id, user_id=owner, text=long_text, privacy_level=PrivacyLevel.L1
    )
    await service.drain_background_work()

    async with database.sessions() as session:
        chunk_steps = list(
            await session.scalars(
                _select(JobStepRecord).where(JobStepRecord.name.like("memory-chunk:%"))
            )
        )
        records = list(await session.scalars(_select(_MemoryRecord)))
    # 同一事实经 4 个块重复陈述：块级落库 + 同消息防重建 → 只存 1 条。
    assert len(chunk_steps) >= 4 and all(step.status == "completed" for step in chunk_steps)
    assert len(records) == 1 and records[0].tier == "core"
    messages = await service.list_messages(conversation.id, user_id=owner)
    receipt = cast(dict[str, Any], (messages[-1].decision_meta or {})["memory_receipt"])
    assert receipt["state"] == "completed" and receipt["supported"] == 3

    # 开关关闭：新回合不再记块游标（原有整消息重试路径）。
    monkeypatch.setenv("ARIA_MEMORY_CHUNK_CURSOR", "0")
    plain_payload = json.dumps(
        {
            "candidates": [
                {"type": "semantic", "content": "用户周末学习合成钢琴曲", "confidence": 0.9}
            ]
        },
        ensure_ascii=False,
    )
    router2 = ScriptedRouter(["好的。", *([plain_payload] * 5)])
    service2 = await _chat_service(database, tmp_path, router2, extractor=LlmMemoryExtractor())
    conversation2 = await service2.create_conversation(user_id=owner, title="synthetic no-cursor")
    await service2.send_message(
        conversation2.id, user_id=owner, text=long_text, privacy_level=PrivacyLevel.L1
    )
    await service2.drain_background_work()
    async with database.sessions() as session:
        latest_job = await session.scalar(
            _select(JobRecord)
            .where(JobRecord.kind == "chat.memory")
            .order_by(JobRecord.id.desc())
            .limit(1)
        )
        latest_job_steps = list(
            await session.scalars(
                _select(JobStepRecord).where(
                    JobStepRecord.job_id == latest_job.id,
                    JobStepRecord.name.like("memory-chunk:%"),
                )
            )
        )
    assert not latest_job_steps


async def test_admin_embedding_status_and_health_block(database: Database, tmp_path: Path) -> None:
    """方案 A2：嵌入换版观测端点与 healthz 块（缓存缺失时字段如实为空）。"""
    from httpx import ASGITransport, AsyncClient
    from test_chat import config_yaml
    from test_memory import candidate

    from app.config import DatabaseConfigStore
    from app.main import create_app

    owner, _, _ = await seed(database)
    path = tmp_path / "hub.yaml"
    path.write_text(config_yaml())
    app = create_app(
        database,
        config_store=DatabaseConfigStore(database, path),
        watch_config=False,
        admin_token="fixture-token",
    )
    headers = {"Authorization": "Bearer fixture-token"}
    app_store = app.state.memory_store
    await MemoryStore(database).add(candidate("synthetic embedding status"), user_id=owner)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/api/v1/admin/memories/embedding-status")).status_code == 401
        status = (
            await client.get("/api/v1/admin/memories/embedding-status", headers=headers)
        ).json()
        assert status["version"] == "char-ngram-hash/1"
        assert status["model"] == "builtin.hashing" and status["dimension"] == 256
        assert status["total_rows"] >= 1
        assert status["stale_rows"] is None and status["pending_version"] is None
        assert "embedding" not in (await client.get("/healthz")).json()
        # 门控/回填运行时缓存写入后，healthz 与端点如实暴露
        app_store.embedding_stale_rows = 3
        app_store.pending_embedding_version = "text-embedding-test/1"
        refreshed = (
            await client.get("/api/v1/admin/memories/embedding-status", headers=headers)
        ).json()
        assert refreshed["pending_version"] == "text-embedding-test/1"
        assert refreshed["stale_rows"] == 3  # 进程内缓存值
        assert refreshed["pending_stale_rows"] == 1  # 待换版本的真实库内计数
        health = (await client.get("/healthz")).json()
        assert health["embedding"] == {
            "version": "char-ngram-hash/1",
            "pending_version": "text-embedding-test/1",
            "stale_rows": 3,
        }
        app_store.embedding_stale_rows = None
        app_store.pending_embedding_version = None


async def test_admin_create_with_explicit_tier(database: Database, tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient
    from test_chat import config_yaml

    from app.config import DatabaseConfigStore
    from app.main import create_app

    owner, _, _ = await seed(database)
    path = tmp_path / "hub.yaml"
    path.write_text(config_yaml())
    app = create_app(
        database,
        config_store=DatabaseConfigStore(database, path),
        watch_config=False,
        admin_token="fixture-token",
    )
    headers = {"Authorization": "Bearer fixture-token"}
    body = {
        "user_id": str(owner),
        "type": "semantic",
        "content": "用户的女儿叫小晴",
        "tier": "core",
    }
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        created = await client.post("/api/v1/admin/memories", json=body, headers=headers)
        assert created.status_code == 201 and created.json()["tier"] == "core"
        events = (
            await client.get(
                f"/api/v1/admin/memories/{created.json()['id']}/lifecycle-events", headers=headers
            )
        ).json()
        assert events[0]["reason"] == "manual" and events[0]["new_tier"] == "core"
        plain = await client.post(
            "/api/v1/admin/memories",
            json={**body, "content": "用户养了一只猫", "tier": None},
            headers=headers,
        )
        assert plain.status_code == 201 and plain.json()["tier"] == "medium"
        plain_events = (
            await client.get(
                f"/api/v1/admin/memories/{plain.json()['id']}/lifecycle-events", headers=headers
            )
        ).json()
        assert not plain_events
