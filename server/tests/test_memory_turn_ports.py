"""Detached completed-turn policy retains privacy, sources and legacy injection."""

import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from test_memory_turn_snapshot import NOW, MessageExtractor, memory

from app.ids import uuid7
from app.llm.contracts import CompletionRequest, CompletionResult
from app.memory.extraction import TurnMemoryExtractor
from app.memory.models import MemoryCandidate, MemorySourceRef
from app.memory.turn_core import CompletedTurnMemoryExtractor
from app.schemas import PrivacyLevel
from scripts.check_architecture import allowed


def inputs(privacy: PrivacyLevel = PrivacyLevel.L1) -> dict[str, Any]:
    return {
        "user_text": "你的身高是多少?",
        "user_message_id": uuid7(),
        "user_occurred_at": NOW,
        "assistant_text": "我的身高是160厘米。",
        "assistant_message_id": uuid7(),
        "assistant_occurred_at": NOW,
        "privacy_level": privacy,
    }


@pytest.mark.parametrize("privacy", list(PrivacyLevel))
async def test_turn_pure_port_keeps_privacy_and_zero_l3_calls(privacy: PrivacyLevel) -> None:
    port = MessageExtractor()
    port.values = [MemoryCandidate(type="episodic", content="合成事件", privacy_level=privacy)]
    result = await CompletedTurnMemoryExtractor(port).extract_turn(**inputs(privacy))
    assert port.entered.is_set() == (privacy != PrivacyLevel.L3)
    if privacy == PrivacyLevel.L3:
        assert result == []
    elif privacy == PrivacyLevel.L2:
        assert len(result) == 1 and result[0].type == "episodic"
    else:
        assert any(item.fact_key == "profile.height" for item in result)


@pytest.mark.parametrize("privacy", [PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2])
async def test_turn_output_owns_nested_candidate_refs(privacy: PrivacyLevel) -> None:
    port = MessageExtractor()
    port.values = [
        MemoryCandidate(
            type="episodic",
            content="合成事件",
            privacy_level=privacy,
            sources=[MemorySourceRef(source_kind="message", source_id=str(uuid7()))],
        )
    ]
    result = await CompletedTurnMemoryExtractor(port).extract_turn(**inputs(privacy))
    assert result[0] is not port.values[0] and result[0].sources[0] is not port.values[0].sources[0]
    port.values[0].sources.clear()
    assert len(result[0].sources) == 1
    result[0].sources.append(MemorySourceRef(source_kind="manual", source_id="fixture"))
    assert port.values[0].sources == []


@pytest.mark.parametrize("change", ["clear", "append"])
async def test_pure_turn_snapshot_is_independent_of_waiting_caller(change: str) -> None:
    port = MessageExtractor()
    port.wait = True
    memories = [memory()] if change == "clear" else []
    task = asyncio.create_task(
        CompletedTurnMemoryExtractor(port).extract_turn(**inputs(), retrieved_memories=memories)
    )
    try:
        await asyncio.wait_for(port.entered.wait(), 3)
        if change == "clear":
            memories.clear()
        else:
            memories.append(memory())
        port.release.set()
        result = await asyncio.wait_for(task, 3)
        assert any(item.fact_key == "profile.height" for item in result) == (change == "append")
    finally:
        port.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("error", [RuntimeError("fixture"), asyncio.CancelledError()])
async def test_turn_message_port_errors_do_not_start_rule_fallback(error: BaseException) -> None:
    class Failed:
        async def extract(
            self,
            text: str,
            *,
            message_id: UUID,
            privacy_level: PrivacyLevel,
            occurred_at: datetime,
        ) -> list[MemoryCandidate]:
            raise error

    with pytest.raises(type(error)) as failure:
        await CompletedTurnMemoryExtractor(Failed()).extract_turn(**inputs())
    assert failure.value is error


async def test_legacy_turn_passes_original_backend_and_message_arguments() -> None:
    class Backend:
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            raise AssertionError("Turn rules must not create an additional model call")

    class Message(MessageExtractor):
        seen: tuple[str, UUID, PrivacyLevel, datetime] | None = None

        async def extract(self, text: str, **kwargs: Any) -> list[MemoryCandidate]:
            self.seen = (text, kwargs["message_id"], kwargs["privacy_level"], kwargs["occurred_at"])
            return await super().extract(text, **kwargs)

    backend, message, request = Backend(), Message(), inputs()
    result = await TurnMemoryExtractor(message).extract_turn(**request, backend=backend)
    assert message.backend is backend
    assert message.seen == (
        request["user_text"],
        request["user_message_id"],
        PrivacyLevel.L1,
        request["user_occurred_at"],
    )
    assert result[0].sources[0].source_id == str(request["assistant_message_id"])
    assert result[0].valid_from == request["assistant_occurred_at"]


async def test_pure_and_legacy_offline_turn_projections_match() -> None:
    request = inputs()
    request.update(
        user_text="记住，你的生日是12月27日。以后我们周五晚上一起看电影。",
        assistant_text="我身高160厘米，体重48公斤，三围82-60-86。我喜欢桂花味的甜点。",
    )
    pure = await CompletedTurnMemoryExtractor().extract_turn(**request)
    legacy = await TurnMemoryExtractor().extract_turn(**request)
    assert [item.model_dump() for item in pure] == [item.model_dump() for item in legacy]
    assert {item.subject_kind for item in pure} == {"assistant", "shared"}
    facts = {item.fact_key: item.content for item in pure if item.fact_key}
    assert facts["profile.height"] == "助手身高为 160 厘米"
    assert facts["profile.weight"] == "助手体重为 48 公斤"
    assert facts["profile.measurements"] == "助手三围为 82-60-86 厘米"
    assert facts["preference.food"] == "助手喜欢桂花味的甜点"


@pytest.mark.parametrize(
    "adapter", ["app.db", "app.llm", "app.memory.store", "sqlalchemy", "httpx", "openai"]
)
def test_pure_turn_cannot_import_runtime_adapters(adapter: str) -> None:
    assert not allowed("app.memory.turn_core", adapter)
