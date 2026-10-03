"""Completed-turn rules own their evidence snapshot and candidate collection."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.ids import uuid7
from app.memory.extraction import ExtractionBackend, TurnMemoryExtractor
from app.memory.models import MemoryCandidate, MemoryEntry, MemorySourceRef
from app.schemas import PrivacyLevel

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def memory() -> MemoryEntry:
    return MemoryEntry(
        id=1,
        user_id=uuid7(),
        subject_kind="assistant",
        subject_key="assistant:primary",
        fact_key="profile.height",
        origin_kind="assistant_statement",
        type="semantic",
        content="助手身高为 160 厘米",
        summary=None,
        privacy_level="L1",
        importance=0.5,
        pin=False,
        status="active",
        confidence=None,
        valid_from=NOW,
        valid_to=None,
        superseded_by=None,
        supersede_reason=None,
        conflict_with=None,
        extractor_version="fixture",
        created_by="fixture",
        created_at=NOW,
        updated_at=NOW,
        last_accessed_at=None,
        access_count=0,
        embedding_model=None,
        embedding_dimension=None,
        embedding_version=None,
    )


class MessageExtractor:
    def __init__(self) -> None:
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.values: list[MemoryCandidate] = []
        self.wait = False
        self.backend: ExtractionBackend | None = None

    async def extract(
        self,
        text: str,
        *,
        message_id: UUID,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
        backend: ExtractionBackend | None = None,
    ) -> list[MemoryCandidate]:
        self.backend = backend
        self.entered.set()
        if self.wait:
            await self.release.wait()
        return self.values


async def extracted(
    extractor: TurnMemoryExtractor, memories: list[MemoryEntry]
) -> list[MemoryCandidate]:
    return await extractor.extract_turn(
        user_text="你的身高是多少?",
        user_message_id=uuid7(),
        user_occurred_at=NOW,
        assistant_text="我的身高是160厘米。",
        assistant_message_id=uuid7(),
        assistant_occurred_at=NOW,
        privacy_level=PrivacyLevel.L1,
        retrieved_memories=memories,
    )


@pytest.mark.parametrize("change", ["clear", "append"])
async def test_turn_echo_uses_retrieved_snapshot_before_wait(change: str) -> None:
    port = MessageExtractor()
    port.wait = True
    memories = [memory()] if change == "clear" else []
    task = asyncio.create_task(extracted(TurnMemoryExtractor(port), memories))
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


async def test_turn_rules_do_not_extend_message_extractor_candidates() -> None:
    port = MessageExtractor()
    original = MemoryCandidate(
        type="semantic",
        content="用户喜欢合成水果",
        privacy_level="L1",
        sources=[MemorySourceRef(source_kind="manual", source_id="fixture")],
    )
    snapshot = original.model_dump()
    port.values.append(original)
    result = await extracted(TurnMemoryExtractor(port), [])
    assert len(result) == 2 and any(item.fact_key == "profile.height" for item in result)
    assert port.values == [original]
    result[0].sources.clear()
    assert port.values[0].model_dump() == snapshot
