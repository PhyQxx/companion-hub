from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select

from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.db import AppUserRecord, ConversationRecord, Database, MessageRecord
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.llm.factory import build_router
from app.llm.provider import EnvSecretProvider
from app.schemas import PrivacyLevel

MAX_CONTEXT_MESSAGES = 20


class CompletionBackend(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...


@dataclass(frozen=True, slots=True)
class ConversationView:
    id: UUID
    user_id: UUID
    title: str | None
    status: str
    last_seq: int
    created_at: datetime
    last_active_at: datetime


@dataclass(frozen=True, slots=True)
class MessageView:
    id: UUID
    conversation_id: UUID
    turn_id: UUID
    seq: int
    role: str
    content: str
    privacy_level: str
    generation_id: UUID | None
    decision_meta: dict[str, object] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChatTurn:
    user_message: MessageView
    assistant_message: MessageView


class ChatService:
    def __init__(
        self,
        database: Database,
        config_store: ConfigStore | DatabaseConfigStore,
        *,
        router_builder: Callable[[HubConfig], CompletionBackend] | None = None,
    ) -> None:
        self._database = database
        self._config_store = config_store
        secrets = EnvSecretProvider()
        self._router_builder = router_builder or (
            lambda config: build_router(config, secrets)
        )

    async def create_conversation(
        self,
        *,
        user_id: UUID,
        title: str | None,
    ) -> ConversationView:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            user = await session.get(AppUserRecord, user_id)
            if user is None or user.status != "active":
                raise LookupError("active user not found")
            record = ConversationRecord(
                id=uuid7(),
                user_id=user_id,
                title=title,
                status="active",
                last_seq=0,
                created_at=now,
                last_active_at=now,
            )
            session.add(record)
        return self._conversation_view(record)

    async def list_conversations(
        self, *, user_id: UUID, limit: int = 50
    ) -> list[ConversationView]:
        query = (
            select(ConversationRecord)
            .where(ConversationRecord.user_id == user_id)
            .order_by(ConversationRecord.last_active_at.desc())
            .limit(limit)
        )
        async with self._database.sessions() as session:
            records = list(await session.scalars(query))
        return [self._conversation_view(record) for record in records]

    async def list_messages(
        self, conversation_id: UUID, *, user_id: UUID, limit: int = 100
    ) -> list[MessageView]:
        async with self._database.sessions() as session:
            conversation = await session.get(ConversationRecord, conversation_id)
            if conversation is None or conversation.user_id != user_id:
                raise LookupError("conversation not found")
            records = list(
                await session.scalars(
                    select(MessageRecord)
                    .where(MessageRecord.conversation_id == conversation_id)
                    .order_by(MessageRecord.seq.desc())
                    .limit(limit)
                )
            )
        records.reverse()
        return [self._message_view(record) for record in records]

    async def send_message(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
    ) -> ChatTurn:
        if privacy_level is PrivacyLevel.L3:
            raise ValueError("L3 durable chat is not allowed")
        now = datetime.now(UTC)
        turn_id = uuid7()
        async with self._database.sessions.begin() as session:
            conversation = await session.scalar(
                select(ConversationRecord)
                .where(ConversationRecord.id == conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.user_id != user_id:
                raise LookupError("conversation not found")
            if conversation.status != "active":
                raise ValueError("conversation is archived")
            conversation.last_seq += 1
            conversation.last_active_at = now
            user_record = MessageRecord(
                id=uuid7(),
                conversation_id=conversation_id,
                turn_id=turn_id,
                seq=conversation.last_seq,
                role="user",
                content=text,
                privacy_level=privacy_level.value,
                created_at=now,
            )
            session.add(user_record)

        history = await self._context_messages(conversation_id)
        snapshot = self._config_store.current
        request = CompletionRequest(
            trace_id=turn_id,
            messages=[
                LLMMessage(
                    role="system",
                    content=(
                        "你是 Aria: 一个可靠、自然的个人陪伴助手。"
                        "直接回答用户，不要声称拥有未提供的记忆或能力。"  # noqa: RUF001
                    ),
                ),
                *[
                    LLMMessage(role=message.role, content=message.content)
                    for message in history
                    if message.role in {"user", "assistant"}
                ],
            ],
            privacy_level=privacy_level,
            route=LLMRoute.DIALOGUE,
            max_tokens=512,
            temperature=0.7,
        )
        result = await self._router_builder(snapshot.config).complete(request)
        generation_id = uuid7()
        decision_meta: dict[str, object] = {
            "schema_version": 1,
            "config_version": snapshot.version,
            "endpoint": result.endpoint,
            "provider": result.provider,
            "model": result.model,
            "route": result.route,
            "finish_reason": result.finish_reason,
            "usage": result.usage.model_dump(mode="json"),
            "latency_ms": result.latency_ms,
        }
        assistant_time = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            conversation = await session.scalar(
                select(ConversationRecord)
                .where(ConversationRecord.id == conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.user_id != user_id:
                raise LookupError("conversation not found")
            conversation.last_seq += 1
            conversation.last_active_at = assistant_time
            assistant_record = MessageRecord(
                id=uuid7(),
                conversation_id=conversation_id,
                turn_id=turn_id,
                seq=conversation.last_seq,
                role="assistant",
                content=result.text,
                privacy_level=privacy_level.value,
                generation_id=generation_id,
                decision_meta=decision_meta,
                created_at=assistant_time,
            )
            session.add(assistant_record)
        return ChatTurn(
            user_message=self._message_view(user_record),
            assistant_message=self._message_view(assistant_record),
        )

    async def _context_messages(self, conversation_id: UUID) -> list[MessageRecord]:
        async with self._database.sessions() as session:
            records = list(
                await session.scalars(
                    select(MessageRecord)
                    .where(MessageRecord.conversation_id == conversation_id)
                    .order_by(MessageRecord.seq.desc())
                    .limit(MAX_CONTEXT_MESSAGES)
                )
            )
        records.reverse()
        return records

    @staticmethod
    def _conversation_view(record: ConversationRecord) -> ConversationView:
        return ConversationView(
            id=record.id,
            user_id=record.user_id,
            title=record.title,
            status=record.status,
            last_seq=record.last_seq,
            created_at=_aware(record.created_at),
            last_active_at=_aware(record.last_active_at),
        )

    @staticmethod
    def _message_view(record: MessageRecord) -> MessageView:
        return MessageView(
            id=record.id,
            conversation_id=record.conversation_id,
            turn_id=record.turn_id,
            seq=record.seq,
            role=record.role,
            content=record.content,
            privacy_level=record.privacy_level,
            generation_id=record.generation_id,
            decision_meta=record.decision_meta,
            created_at=_aware(record.created_at),
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
