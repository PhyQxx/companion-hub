from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Select, delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from app.db import (
    ConversationRecord,
    Database,
    EventRecord,
    MessageRecord,
    TimelineEventRecord,
)
from app.schemas.common import PrivacyLevel

from .models import (
    TimelineActor,
    TimelineEvent,
    TimelineEvidence,
    TimelineSearchResult,
    TimelineSourceType,
)

MAX_INDEX_SUMMARY_CHARS = 320
MAX_SOURCE_EXPANSION = 8


class TimelineStore:
    """Derived history index. Source rows remain authoritative."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    async def index_completed_turn(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        user_message_id: UUID,
        user_text: str,
        user_occurred_at: datetime,
        assistant_message_id: UUID,
        assistant_text: str,
        assistant_occurred_at: datetime,
        privacy_level: PrivacyLevel,
    ) -> tuple[TimelineEvent, ...]:
        privacy = PrivacyLevel(privacy_level)
        if privacy is PrivacyLevel.L3:
            return ()
        created: list[TimelineEvent] = []
        for actor, source_id, text, occurred_at in (
            (TimelineActor.USER, user_message_id, user_text, user_occurred_at),
            (TimelineActor.ASSISTANT, assistant_message_id, assistant_text, assistant_occurred_at),
        ):
            item = await self.index_message(
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=source_id,
                actor=actor,
                text=text,
                privacy_level=privacy,
                occurred_at=occurred_at,
            )
            if item is not None:
                created.append(item)
        return tuple(created)

    async def index_message(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        actor: TimelineActor,
        text: str,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
    ) -> TimelineEvent | None:
        privacy = PrivacyLevel(privacy_level)
        if privacy is PrivacyLevel.L3:
            return None
        summary = (
            _index_summary(text)
            if privacy in {PrivacyLevel.L0, PrivacyLevel.L1}
            else "L2 对话消息"
        )
        record = TimelineEventRecord(
            user_id=user_id,
            occurred_at=_utc(occurred_at),
            source_type=TimelineSourceType.MESSAGE.value,
            source_id=str(message_id),
            actor=TimelineActor(actor).value,
            event_type="conversation.message",
            conversation_id=conversation_id,
            summary=summary,
            privacy_level=privacy.value,
            importance=0.25,
            entities=[],
            keywords=_keywords(summary) if privacy is not PrivacyLevel.L2 else [],
            metadata_json={},
        )
        return await self._insert(record)

    async def index_event_record(self, event: EventRecord) -> TimelineEvent | None:
        record = timeline_record_from_event(event)
        if record is None:
            return None
        return await self._insert(record)

    async def index_screen_observation(
        self,
        *,
        user_id: UUID,
        observation_id: UUID,
        display: int,
        summary: str,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
        metadata: dict[str, Any] | None = None,
        importance: float = 0.3,
    ) -> TimelineEvent | None:
        """记录一次周期屏幕感知的观察摘要（原图即焚，只存分析文本）。"""
        privacy = PrivacyLevel(privacy_level)
        if privacy is PrivacyLevel.L3:
            return None
        text = summary.strip()
        if not text:
            return None
        record = TimelineEventRecord(
            user_id=user_id,
            occurred_at=_utc(occurred_at),
            source_type=TimelineSourceType.DEVICE.value,
            source_id=str(observation_id),
            actor=TimelineActor.DEVICE.value,
            event_type="screen.observed",
            title=f"屏幕观察 · 显示器 {display}",
            summary=_index_summary(text),
            privacy_level=privacy.value,
            importance=importance,
            entities=[],
            keywords=_keywords(text)[:8],
            metadata_json={"display": display, **(metadata or {})},
        )
        return await self._insert(record)

    async def index_browser_observation(
        self,
        *,
        user_id: UUID,
        observation_id: UUID,
        origin: str,
        title: str,
        summary: str,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
        metadata: dict[str, Any] | None = None,
        importance: float = 0.3,
    ) -> TimelineEvent | None:
        """Store a browser summary, origin and title, never page text or URL paths."""
        privacy = PrivacyLevel(privacy_level)
        parsed = urlsplit(origin)
        text = summary.strip()
        if privacy is PrivacyLevel.L3 or not text:
            return None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        safe_origin = f"{parsed.scheme}://{parsed.netloc}"
        host = parsed.hostname.casefold()
        record = TimelineEventRecord(
            user_id=user_id,
            occurred_at=_utc(occurred_at),
            source_type=TimelineSourceType.DEVICE.value,
            source_id=str(observation_id),
            actor=TimelineActor.DEVICE.value,
            event_type="browser.observed",
            title=f"浏览观察 · {host}",
            summary=_index_summary(text),
            privacy_level=privacy.value,
            importance=importance,
            entities=[],
            keywords=_keywords(text)[:8],
            metadata_json={
                **(metadata or {}),
                "origin": safe_origin,
                "page_title": title[:200],
            },
        )
        return await self._insert(record)

    async def index_custom(
        self,
        *,
        user_id: UUID,
        source_id: str,
        source_type: TimelineSourceType,
        actor: TimelineActor,
        event_type: str,
        title: str,
        summary: str,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
        metadata: dict[str, Any] | None = None,
        importance: float = 0.4,
    ) -> TimelineEvent | None:
        """通用系统事件索引（SAFE 告警等新事件类型的接入点，避免每类一个方法）。"""
        privacy = PrivacyLevel(privacy_level)
        if privacy is PrivacyLevel.L3:
            return None
        text = summary.strip()
        if not text:
            return None
        record = TimelineEventRecord(
            user_id=user_id,
            occurred_at=_utc(occurred_at),
            source_type=source_type.value,
            source_id=source_id[:255],
            actor=actor.value,
            event_type=event_type,
            title=title[:320],
            summary=_index_summary(text),
            privacy_level=privacy.value,
            importance=importance,
            entities=[],
            keywords=_keywords(text)[:8],
            metadata_json=metadata or {},
        )
        return await self._insert(record)

    async def get(self, timeline_id: int, *, user_id: UUID | None = None) -> TimelineEvent:
        async with self._database.sessions() as session:
            record = await session.get(TimelineEventRecord, timeline_id)
        if record is None or (user_id is not None and record.user_id != user_id):
            raise LookupError(f"timeline event not found: {timeline_id}")
        return _entry(record)

    async def search(
        self,
        *,
        user_id: UUID,
        query: str = "",
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        actors: Sequence[TimelineActor] | None = None,
        source_types: Sequence[TimelineSourceType] | None = None,
        event_types: Sequence[str] | None = None,
        conversation_id: UUID | None = None,
        privacy_levels: Sequence[PrivacyLevel] = (PrivacyLevel.L0, PrivacyLevel.L1),
        limit: int = 20,
        offset: int = 0,
        candidate_limit: int = 300,
    ) -> TimelineSearchResult:
        statement = _event_filter(
            user_id=user_id,
            start_at=start_at,
            end_at=end_at,
            actors=actors,
            source_types=source_types,
            event_types=event_types,
            conversation_id=conversation_id,
            privacy_levels=privacy_levels,
        ).order_by(TimelineEventRecord.occurred_at.desc()).limit(candidate_limit)
        async with self._database.sessions() as session:
            records = list(await session.scalars(statement))

        if not query.strip():
            selected = records[offset : offset + limit]
        else:
            query_tokens = _token_set(query)
            scored = [
                (_score(record, query_tokens), record)
                for record in records
            ]
            scored = [item for item in scored if item[0] > 0]
            scored.sort(key=lambda item: (item[0], item[1].occurred_at), reverse=True)
            selected = [record for _, record in scored[offset : offset + limit]]
        return TimelineSearchResult(
            events=tuple(_entry(record) for record in selected),
            candidate_count=len(records),
        )

    async def count_events(
        self,
        *,
        user_id: UUID,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        actors: Sequence[TimelineActor] | None = None,
        source_types: Sequence[TimelineSourceType] | None = None,
        event_types: Sequence[str] | None = None,
        conversation_id: UUID | None = None,
        privacy_levels: Sequence[PrivacyLevel] = (PrivacyLevel.L0, PrivacyLevel.L1),
    ) -> int:
        """与 search 同过滤条件的精确总数，供分页使用（无文本相关性过滤）。"""
        statement = select(func.count()).select_from(
            _event_filter(
                user_id=user_id,
                start_at=start_at,
                end_at=end_at,
                actors=actors,
                source_types=source_types,
                event_types=event_types,
                conversation_id=conversation_id,
                privacy_levels=privacy_levels,
            ).subquery()
        )
        async with self._database.sessions() as session:
            return int(await session.scalar(statement) or 0)

    async def expand_sources(
        self,
        events: Sequence[TimelineEvent],
        *,
        user_id: UUID,
        privacy_level: PrivacyLevel,
        limit: int = MAX_SOURCE_EXPANSION,
    ) -> tuple[TimelineEvidence, ...]:
        allowed = (
            {PrivacyLevel.L0.value, PrivacyLevel.L1.value, PrivacyLevel.L2.value}
            if privacy_level is PrivacyLevel.L2
            else {PrivacyLevel.L0.value, PrivacyLevel.L1.value}
        )
        evidence: list[TimelineEvidence] = []
        async with self._database.sessions() as session:
            for event in events[:limit]:
                if event.privacy_level not in allowed:
                    continue
                if event.source_type == TimelineSourceType.MESSAGE.value:
                    try:
                        source_uuid = UUID(event.source_id)
                    except ValueError:
                        continue
                    message = await session.get(MessageRecord, source_uuid)
                    if message is None or message.privacy_level not in allowed:
                        continue
                    conversation = await session.get(ConversationRecord, message.conversation_id)
                    if conversation is None or conversation.user_id != user_id:
                        continue
                    evidence.append(
                        TimelineEvidence(
                            timeline_id=event.id,
                            source_type=event.source_type,
                            source_id=event.source_id,
                            occurred_at=event.occurred_at,
                            actor=event.actor,
                            event_type=event.event_type,
                            text=message.content,
                        )
                    )
                elif event.source_type == TimelineSourceType.EVENT.value:
                    try:
                        source_uuid = UUID(event.source_id)
                    except ValueError:
                        continue
                    source = await session.get(EventRecord, source_uuid)
                    if (
                        source is None
                        or source.user_id != user_id
                        or source.privacy_level not in allowed
                    ):
                        continue
                    evidence.append(
                        TimelineEvidence(
                            timeline_id=event.id,
                            source_type=event.source_type,
                            source_id=event.source_id,
                            occurred_at=event.occurred_at,
                            actor=event.actor,
                            event_type=event.event_type,
                            text=_event_summary(source),
                        )
                    )
        return tuple(evidence[:limit])

    async def purge_by_source(
        self, source_type: TimelineSourceType | str, source_ids: Sequence[str]
    ) -> int:
        if not source_ids:
            return 0
        kind = TimelineSourceType(source_type).value
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                delete(TimelineEventRecord).where(
                    TimelineEventRecord.source_type == kind,
                    TimelineEventRecord.source_id.in_(list(source_ids)),
                )
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def purge_conversation(self, conversation_id: UUID) -> int:
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                delete(TimelineEventRecord).where(
                    TimelineEventRecord.conversation_id == conversation_id
                )
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def _insert(self, record: TimelineEventRecord) -> TimelineEvent | None:
        try:
            async with self._database.sessions.begin() as session:
                session.add(record)
                await session.flush()
                await session.refresh(record)
        except IntegrityError:
            return None
        return _entry(record)


def _entry(record: TimelineEventRecord) -> TimelineEvent:
    return TimelineEvent(
        id=record.id,
        user_id=record.user_id,
        occurred_at=_aware(record.occurred_at),
        ended_at=_aware(record.ended_at) if record.ended_at else None,
        source_type=record.source_type,
        source_id=record.source_id,
        actor=record.actor,
        event_type=record.event_type,
        conversation_id=record.conversation_id,
        title=record.title,
        summary=record.summary,
        privacy_level=record.privacy_level,
        importance=record.importance,
        entities=tuple(record.entities or []),
        keywords=tuple(record.keywords or []),
        metadata=dict(record.metadata_json or {}),
        created_at=_aware(record.created_at),
    )


def timeline_record_from_event(event: EventRecord) -> TimelineEventRecord | None:
    privacy = PrivacyLevel(event.privacy_level)
    if privacy is PrivacyLevel.L3:
        return None
    summary = (
        _event_summary(event)
        if privacy in {PrivacyLevel.L0, PrivacyLevel.L1}
        else f"L2 事件 {event.type}"
    )
    return TimelineEventRecord(
        user_id=event.user_id,
        occurred_at=_utc(event.occurred_at),
        source_type=TimelineSourceType.EVENT.value,
        source_id=str(event.event_id),
        actor=_event_actor(event),
        event_type=event.type,
        conversation_id=event.conversation_id,
        summary=summary,
        privacy_level=privacy.value,
        importance=0.35,
        entities=[],
        keywords=_keywords(summary) if privacy is not PrivacyLevel.L2 else [],
        metadata_json={"schema_ref": event.schema_ref},
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _utc(value: datetime) -> datetime:
    aware = _aware(value)
    return aware.astimezone(UTC)


def _event_filter(
    *,
    user_id: UUID,
    start_at: datetime | None,
    end_at: datetime | None,
    actors: Sequence[TimelineActor] | None,
    source_types: Sequence[TimelineSourceType] | None,
    event_types: Sequence[str] | None,
    conversation_id: UUID | None,
    privacy_levels: Sequence[PrivacyLevel],
) -> Select[tuple[TimelineEventRecord]]:
    statement = select(TimelineEventRecord).where(
        TimelineEventRecord.user_id == user_id,
        TimelineEventRecord.privacy_level.in_([level.value for level in privacy_levels]),
    )
    if start_at is not None:
        statement = statement.where(TimelineEventRecord.occurred_at >= _utc(start_at))
    if end_at is not None:
        statement = statement.where(TimelineEventRecord.occurred_at < _utc(end_at))
    if actors:
        statement = statement.where(
            TimelineEventRecord.actor.in_([TimelineActor(item).value for item in actors])
        )
    if source_types:
        statement = statement.where(
            TimelineEventRecord.source_type.in_(
                [TimelineSourceType(item).value for item in source_types]
            )
        )
    if event_types:
        statement = statement.where(TimelineEventRecord.event_type.in_(list(event_types)))
    if conversation_id is not None:
        statement = statement.where(TimelineEventRecord.conversation_id == conversation_id)
    return statement


def _index_summary(text: str) -> str:
    compact = " ".join(text.split())
    return compact[:MAX_INDEX_SUMMARY_CHARS] or "空消息"


def _event_summary(event: EventRecord) -> str:
    text_parts: list[str] = []
    for part in event.payload.get("content", []):
        if isinstance(part, dict) and part.get("type") in {"text", "speech"}:
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                text_parts.append(text.strip())
    if text_parts:
        return _index_summary(" ".join(text_parts))
    return _index_summary(event.type)


def _event_actor(event: EventRecord) -> str:
    if event.type.startswith("device."):
        return TimelineActor.DEVICE.value
    if event.type.startswith("system."):
        return TimelineActor.SYSTEM.value
    return TimelineActor.EXTERNAL.value


def _keywords(text: str) -> list[str]:
    return sorted(_token_set(text))[:24]


def _token_set(text: str) -> set[str]:
    normalized = text.lower()
    ascii_tokens = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    chinese_tokens = {chinese[index : index + 2] for index in range(max(len(chinese) - 1, 0))}
    if chinese:
        chinese_tokens.add(chinese)
    return ascii_tokens | chinese_tokens


def _score(record: TimelineEventRecord, query_tokens: set[str]) -> float:
    if not query_tokens:
        return 1.0
    haystack = _token_set(
        " ".join(
            [
                record.title or "",
                record.summary,
                record.event_type,
                " ".join(record.keywords or []),
            ]
        )
    )
    if not haystack:
        return 0.0
    overlap = len(query_tokens & haystack)
    if overlap == 0:
        return 0.0
    return overlap / max(len(query_tokens), 1) + record.importance * 0.05
