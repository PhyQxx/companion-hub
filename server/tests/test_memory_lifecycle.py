from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
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
