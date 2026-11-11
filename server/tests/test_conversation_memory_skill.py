from pathlib import Path

import pytest
from test_chat import config_yaml, create_user

from app.chat import ChatService
from app.config import DatabaseConfigStore
from app.db import Base, MessageRecord, create_database
from app.ids import uuid7
from app.memory import MemoryIngester, MemoryStore
from app.schemas import PrivacyLevel
from app.skills.conversation_memory import (
    NAME,
    ConversationMemoryTool,
    ReviewArguments,
    ReviewItem,
    ensure_conversation_memory_skill,
)
from app.skills.store import SkillStore
from app.tools.contracts import ToolContext


@pytest.fixture
async def setup(tmp_path: Path):
    db = create_database(f"sqlite+aiosqlite:///{tmp_path}/review.db")
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    path = tmp_path / "hub.yaml"
    path.write_text(config_yaml())
    config = DatabaseConfigStore(db, path)
    await config.load()
    memories = MemoryStore(db)
    service = ChatService(db, config, memory_store=memories)
    user = await create_user(db)
    other = await create_user(db, "Other")
    conversation = await service.create_conversation(user_id=user.id, title="Review")
    async with db.sessions.begin() as session:
        for seq in range(1, 146):
            session.add(
                MessageRecord(
                    id=uuid7(),
                    conversation_id=conversation.id,
                    turn_id=uuid7(),
                    seq=seq,
                    role="user",
                    content=f"我喜欢阅读第{seq}类书籍",
                    privacy_level="L2" if seq == 2 else "L1",
                )
            )
    skills = SkillStore(db)
    await ensure_conversation_memory_skill(skills)
    tool = ConversationMemoryTool(service, skills, MemoryIngester(memories))
    yield tool, user, other, conversation, skills, memories
    await db.close()


async def test_full_pagination_privacy_and_owner(setup):
    tool, user, other, conv, _, _ = setup
    ctx = ToolContext(user_id=user.id, privacy_level=PrivacyLevel.L1)
    seq = 0
    seen = []
    while True:
        result = await tool.execute(
            ReviewArguments(
                operation="read", conversation_id=conv.id, after_seq=seq, until_seq=145
            ),
            ctx,
        )
        assert result.ok
        seen.extend(result.data["messages"])
        seq = result.data["next_seq"]
        if result.data["done"]:
            break
    assert [m["seq"] for m in seen] == list(range(1, 146))
    assert seen[1]["content"] is None
    denied = await tool.execute(
        ReviewArguments(operation="read", conversation_id=conv.id),
        ToolContext(user_id=other.id, privacy_level=PrivacyLevel.L1),
    )
    assert not denied.ok


async def test_save_support_skip_and_invalid_source(setup):
    tool, user, _, conv, skills, _ = setup
    ctx = ToolContext(user_id=user.id, privacy_level=PrivacyLevel.L1)
    args = ReviewArguments(
        operation="review",
        conversation_id=conv.id,
        until_seq=145,
        items=[
            ReviewItem(
                source_seq=1,
                decision="remember",
                quote="我喜欢阅读第1类书籍",
                content="用户喜欢阅读第1类书籍",
            ),
            ReviewItem(source_seq=2, decision="remember", quote="我喜欢", content="私密事实"),
            ReviewItem(source_seq=3, decision="skip", reason="transient"),
            ReviewItem(source_seq=4, decision="remember", quote="不存在的原文", content="伪造事实"),
        ],
    )
    first = await tool.execute(args, ctx)
    assert [r["decision"] for r in first.data["results"]] == [
        "created",
        "not_saved",
        "not_saved",
        "not_saved",
    ]
    second = await tool.execute(args, ctx)
    assert second.data["results"][0]["decision"] == "supported"
    assert second.data["results"][0]["memory_id"] == first.data["results"][0]["memory_id"]
    skill = await skills.get_by_name(NAME)
    await skills.set_enabled(skill.id, False)
    await ensure_conversation_memory_skill(skills)
    assert not (await tool.execute(args, ctx)).ok


async def test_background_whole_conversation_and_status_owner(setup):
    import json

    from app.llm import CompletionResult, LLMRoute, ModelUsage

    tool, user, other, _conv, _, _ = setup
    seen = []

    class Router:
        async def complete(self, request):
            current = json.loads(request.messages[-1].content)["current"]
            seen.append(current["seq"])
            return CompletionResult(
                text=json.dumps(
                    {
                        "items": [
                            {
                                "source_seq": current["seq"],
                                "decision": "skip",
                                "reason": "transient",
                            }
                        ]
                    }
                ),
                provider="test",
                model="test",
                endpoint="test",
                route=LLMRoute.UTILITY,
                finish_reason="stop",
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                latency_ms=0,
            )

    tool.service._router_builder = lambda config: Router()
    ctx = ToolContext(user_id=user.id, privacy_level=PrivacyLevel.L1)
    started = await tool.execute(ReviewArguments(operation="start", title="Review"), ctx)
    assert started.ok
    job_id = started.data["job_id"]
    await tool.service._memory_review_worker.drain_ready()
    status = await tool.execute(ReviewArguments(operation="status", job_id=job_id), ctx)
    assert status.data["status"] == "succeeded"
    assert seen == [seq for seq in range(1, 146) if seq != 2]
    denied = await tool.execute(
        ReviewArguments(operation="status", job_id=job_id),
        ToolContext(user_id=other.id, privacy_level=PrivacyLevel.L1),
    )
    assert not denied.ok


async def test_worker_partial_model_failure_is_not_success(setup):
    import json

    from app.llm import CompletionResult, LLMRoute, ModelUsage

    tool, user, _other, conv, _, _ = setup

    class Router:
        async def complete(self, request):
            row = json.loads(request.messages[-1].content)["current"]
            if row["seq"] == 3:
                raise TimeoutError("private provider error")
            return CompletionResult(
                text=json.dumps(
                    {
                        "items": [
                            {
                                "source_seq": row["seq"],
                                "decision": "remember",
                                "quote": row["content"],
                                "content": "用户喜欢阅读第1类书籍",
                            }
                        ]
                    }
                ),
                provider="test",
                model="test",
                endpoint="test",
                route=LLMRoute.UTILITY,
                finish_reason="stop",
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                latency_ms=0,
            )

    tool.service._router_builder = lambda config: Router()
    worker = tool.service._memory_review_worker
    job = await worker.engine.submit(
        NAME,
        {"conversation_id": str(conv.id), "until_seq": 3},
        owner=str(user.id),
        resource_class=NAME,
        max_attempts=1,
    )
    await worker.drain_ready()
    current = await worker.engine.get(job.id)
    assert current.status == "failed"
    detail = await worker.engine.failure_detail(job.id, owner=str(user.id))
    assert detail["failed_count"] == 1
    assert detail["failures"][0]["message_seq"] == 3
    assert detail["failures"][0]["stage"] == "model_call"
    assert detail["failures"][0]["error_type"] == "TimeoutError"
    assert "private provider error" not in str(detail)
    assert await worker.engine.failure_detail(job.id, owner=str(_other.id)) is None

    memories = await tool.ingester._store.list_memories(user_id=user.id)
    assert len(memories) == 1


async def test_initialization_failure_has_no_invented_message_position(setup):
    tool, user, _other, conv, _, _ = setup

    def broken_router(config):
        raise ValueError("secret configuration payload")

    tool.service._router_builder = broken_router
    worker = tool.service._memory_review_worker
    job = await worker.engine.submit(
        NAME,
        {"conversation_id": str(conv.id), "until_seq": 140},
        owner=str(user.id),
        resource_class=NAME,
        max_attempts=1,
    )
    await worker.drain_ready()
    status = await tool.execute(
        ReviewArguments(operation="status", job_id=job.id),
        ToolContext(user_id=user.id, privacy_level=PrivacyLevel.L1),
    )
    assert status.data["status"] == "failed"
    assert status.data["scope"]["until_seq"] == 140
    assert status.data["failure_detail"]["stage"] == "build_router"
    assert status.data["failure_detail"]["message_seq"] is None
    assert status.data["failure_detail"]["error_type"] == "ValueError"
    assert "secret configuration payload" not in str(status.data)
