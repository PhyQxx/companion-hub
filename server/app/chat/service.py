# ruff: noqa: RUF002, RUF003
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, select, update

from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.db import (
    AppUserRecord,
    ConversationRecord,
    Database,
    InteractionTurnRecord,
    MessageRecord,
)
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.llm.factory import build_router
from app.llm.provider import EnvSecretProvider
from app.memory import (
    DeletionReceipt,
    ExtractionBackend,
    MemoryExtractor,
    MemoryIngester,
    MemoryRetriever,
    MemoryStore,
    RetrievalResult,
)
from app.persona import PersonaConfig, PersonaStore
from app.schemas import PrivacyLevel

from .reply import ControlStreamFilter, parse_agent_reply, structured_reply_instruction

MAX_CONTEXT_MESSAGES = 20

logger = logging.getLogger(__name__)


class CompletionBackend(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...

    async def stream(
        self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
    ) -> CompletionResult: ...


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


@dataclass(frozen=True, slots=True)
class PendingTurn:
    turn_id: UUID
    generation_id: UUID
    conversation_id: UUID
    user_id: UUID
    turn_seq: int
    user_message: MessageView
    request: CompletionRequest
    config: HubConfig
    config_version: int
    persona: PersonaConfig
    persona_version: int
    memory_retrieval: RetrievalResult | None = None


class TurnCancelled(RuntimeError):
    pass


class ChatService:
    def __init__(
        self,
        database: Database,
        config_store: ConfigStore | DatabaseConfigStore,
        *,
        router_builder: Callable[[HubConfig], CompletionBackend] | None = None,
        persona_store: PersonaStore | None = None,
        memory_store: MemoryStore | None = None,
        memory_extractor: MemoryExtractor | None = None,
    ) -> None:
        self._database = database
        self._config_store = config_store
        self._persona_store = persona_store
        self._memory_store = memory_store
        self._memory_retriever = MemoryRetriever(memory_store) if memory_store else None
        self._memory_ingester = (
            MemoryIngester(memory_store, extractor=memory_extractor)
            if memory_store
            else None
        )
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
                last_turn_seq=0,
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
        pending = await self.start_turn(
            conversation_id,
            user_id=user_id,
            text=text,
            privacy_level=privacy_level,
        )
        await self._transition(pending.turn_id, {"accepted"}, "thinking")
        try:
            backend = self._router_builder(pending.config)
            result = await backend.complete(pending.request)
            return await self._commit_turn(pending, result, backend=backend)
        except BaseException:
            await self._fail_if_active(pending.turn_id)
            raise

    async def start_turn(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
    ) -> PendingTurn:
        if privacy_level is PrivacyLevel.L3:
            raise ValueError("L3 durable chat is not allowed")
        now = datetime.now(UTC)
        turn_id = uuid7()
        generation_id = uuid7()
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
            conversation.last_turn_seq += 1
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
            await session.flush()
            session.add(
                InteractionTurnRecord(
                    id=turn_id,
                    conversation_id=conversation_id,
                    turn_seq=conversation.last_turn_seq,
                    generation_id=generation_id,
                    state="accepted",
                    state_version=1,
                    input_message_id=user_record.id,
                    created_at=now,
                )
            )

        history = await self._context_messages(conversation_id)
        snapshot = self._config_store.current
        persona_snapshot = self._persona_store.current if self._persona_store else None
        persona = persona_snapshot.persona if persona_snapshot else PersonaConfig()
        memory_retrieval: RetrievalResult | None = None
        memory_block = ""
        if self._memory_retriever is not None:
            memory_retrieval = await self._memory_retriever.retrieve(
                text, user_id=user_id, privacy_level=privacy_level
            )
            memory_block = MemoryRetriever.render_context(memory_retrieval)
        request = CompletionRequest(
            trace_id=turn_id,
            messages=[
                LLMMessage(
                    role="system",
                    content=persona.render_system_prompt()
                    + structured_reply_instruction(persona)
                    + (f"\n\n{memory_block}" if memory_block else ""),
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
        return PendingTurn(
            turn_id=turn_id,
            generation_id=generation_id,
            conversation_id=conversation_id,
            user_id=user_id,
            turn_seq=conversation.last_turn_seq,
            user_message=self._message_view(user_record),
            request=request,
            config=snapshot.config,
            config_version=snapshot.version,
            persona=persona,
            persona_version=persona_snapshot.version if persona_snapshot else 0,
            memory_retrieval=memory_retrieval,
        )

    async def run_stream(
        self,
        pending: PendingTurn,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> ChatTurn:
        await self._transition(pending.turn_id, {"accepted"}, "thinking")
        emitted = False
        stream_filter = ControlStreamFilter()

        async def guarded_delta(delta: str) -> None:
            nonlocal emitted
            if not emitted:
                await self._transition(pending.turn_id, {"thinking"}, "streaming")
                emitted = True
            elif not await self._turn_is_active(pending.turn_id):
                raise TurnCancelled("generation_cancelled")
            await on_delta(delta)

        async def filtered_delta(delta: str) -> None:
            for visible in stream_filter.feed(delta):
                await guarded_delta(visible)

        try:
            backend = self._router_builder(pending.config)
            result = await backend.stream(pending.request, filtered_delta)
            for visible in stream_filter.finish():
                await guarded_delta(visible)
            return await self._commit_turn(pending, result, backend=backend)
        except BaseException:
            await self._fail_if_active(pending.turn_id)
            raise

    async def cancel_turn(
        self, generation_id: UUID, *, user_id: UUID, reason: str = "user_cancelled"
    ) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            row = (
                await session.execute(
                    select(InteractionTurnRecord, ConversationRecord)
                    .join(
                        ConversationRecord,
                        ConversationRecord.id == InteractionTurnRecord.conversation_id,
                    )
                    .where(InteractionTurnRecord.generation_id == generation_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None or row[1].user_id != user_id:
                raise LookupError("generation not found")
            turn = row[0]
            if turn.state in {"cancelled", "failed", "completed"}:
                return False
            turn.state = "cancelled"
            turn.state_version += 1
            turn.cancel_reason = reason
            turn.completed_at = now
        return True

    async def list_messages_after(
        self, conversation_id: UUID, *, user_id: UUID, after_seq: int, limit: int = 200
    ) -> list[MessageView]:
        async with self._database.sessions() as session:
            conversation = await session.get(ConversationRecord, conversation_id)
            if conversation is None or conversation.user_id != user_id:
                raise LookupError("conversation not found")
            records = list(
                await session.scalars(
                    select(MessageRecord)
                    .where(
                        MessageRecord.conversation_id == conversation_id,
                        MessageRecord.seq > after_seq,
                    )
                    .order_by(MessageRecord.seq)
                    .limit(limit)
                )
            )
        return [self._message_view(record) for record in records]

    async def delete_conversation(
        self, conversation_id: UUID, *, user_id: UUID
    ) -> DeletionReceipt:
        """硬删除会话：连同回合、消息与沉淀记忆一起清除。

        先按消息来源清除记忆链，整个动作以 message/<会话ID> 实体记入
        删除台账；备份恢复后可按台账重放，被删内容不会复活。
        """
        async with self._database.sessions() as session:
            conversation = await session.get(ConversationRecord, conversation_id)
            if conversation is None or conversation.user_id != user_id:
                raise LookupError("conversation not found")
            message_ids = [
                str(row)
                for row in await session.scalars(
                    select(MessageRecord.id).where(
                        MessageRecord.conversation_id == conversation_id
                    )
                )
            ]
        receipt = DeletionReceipt(ledger_id=0, entity_id=str(conversation_id), deleted_ids=())
        if self._memory_store is not None:
            receipt = await self._memory_store.hard_delete_by_source(
                "message",
                message_ids,
                entity_kind="message",
                entity_id=str(conversation_id),
                actor="user",
                reason="conversation deleted by user",
                always_record=True,
            )
        async with self._database.sessions.begin() as session:
            conversation = await session.scalar(
                select(ConversationRecord)
                .where(ConversationRecord.id == conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.user_id != user_id:
                raise LookupError("conversation not found")
            await session.execute(
                delete(InteractionTurnRecord).where(
                    InteractionTurnRecord.conversation_id == conversation_id
                )
            )
            await session.execute(
                delete(MessageRecord).where(
                    MessageRecord.conversation_id == conversation_id
                )
            )
            await session.delete(conversation)
        return receipt

    async def recover_incomplete_turns(self) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(InteractionTurnRecord)
                .where(
                    InteractionTurnRecord.state.in_({"accepted", "thinking", "streaming"})
                )
                .values(
                    state="cancelled",
                    state_version=InteractionTurnRecord.state_version + 1,
                    cancel_reason="process_restarted",
                    completed_at=now,
                )
            )

    async def _commit_turn(
        self,
        pending: PendingTurn,
        result: CompletionResult,
        *,
        backend: CompletionBackend | None = None,
    ) -> ChatTurn:
        reply = parse_agent_reply(result.text, pending.persona)
        decision_meta: dict[str, object] = {
            "schema_version": 1,
            "config_version": pending.config_version,
            "persona_version": pending.persona_version,
            "agent_reply": reply.model_dump(mode="json"),
            "endpoint": result.endpoint,
            "provider": result.provider,
            "model": result.model,
            "route": result.route,
            "finish_reason": result.finish_reason,
            "usage": result.usage.model_dump(mode="json"),
            "latency_ms": result.latency_ms,
        }
        if pending.memory_retrieval is not None:
            decision_meta["memory"] = {
                "policy_version": pending.memory_retrieval.policy_version,
                "candidate_count": pending.memory_retrieval.candidate_count,
                "hits": [
                    {
                        "id": hit.memory.id,
                        "type": hit.memory.type,
                        "score": round(hit.final_score, 4),
                        "reasons": list(hit.reasons),
                    }
                    for hit in pending.memory_retrieval.hits
                ],
            }
        assistant_time = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            conversation = await session.scalar(
                select(ConversationRecord)
                .where(ConversationRecord.id == pending.conversation_id)
                .with_for_update()
            )
            turn = await session.scalar(
                select(InteractionTurnRecord)
                .where(InteractionTurnRecord.id == pending.turn_id)
                .with_for_update()
            )
            if conversation is None or conversation.user_id != pending.user_id or turn is None:
                raise LookupError("conversation not found")
            if turn.state == "cancelled":
                raise TurnCancelled("generation_cancelled")
            if turn.state not in {"thinking", "streaming"}:
                raise RuntimeError("turn is not committable")
            conversation.last_seq += 1
            conversation.last_active_at = assistant_time
            assistant_record = MessageRecord(
                id=uuid7(),
                conversation_id=pending.conversation_id,
                turn_id=pending.turn_id,
                seq=conversation.last_seq,
                role="assistant",
                content=reply.text,
                privacy_level=str(pending.request.privacy_level),
                generation_id=pending.generation_id,
                decision_meta=decision_meta,
                created_at=assistant_time,
            )
            session.add(assistant_record)
            turn.state = "completed"
            turn.state_version += 1
            turn.completed_at = assistant_time
        turn_result = ChatTurn(
            user_message=pending.user_message,
            assistant_message=self._message_view(assistant_record),
        )
        await self._consolidate_memory(pending, backend=backend)
        return turn_result

    async def _consolidate_memory(
        self, pending: PendingTurn, *, backend: ExtractionBackend | None = None
    ) -> None:
        if self._memory_ingester is None:
            return
        try:
            await self._memory_ingester.ingest_message(
                user_id=pending.user_id,
                message_id=pending.user_message.id,
                text=pending.user_message.content,
                privacy_level=pending.request.privacy_level,
                occurred_at=pending.user_message.created_at,
                backend=backend,
            )
        except Exception:
            # 已提交的回复绝不能因为记忆沉淀失败而失败；
            # 下一轮会基于自己的消息重新沉淀，这里只记日志
            logger.warning(
                "memory consolidation failed for turn %s", pending.turn_id, exc_info=True
            )

    async def _transition(
        self, turn_id: UUID, from_states: set[str], target: str
    ) -> None:
        async with self._database.sessions.begin() as session:
            turn = await session.scalar(
                select(InteractionTurnRecord)
                .where(InteractionTurnRecord.id == turn_id)
                .with_for_update()
            )
            if turn is None:
                raise LookupError("turn not found")
            if turn.state == "cancelled":
                raise TurnCancelled("generation_cancelled")
            if turn.state not in from_states:
                raise RuntimeError("invalid turn state transition")
            turn.state = target
            turn.state_version += 1

    async def _turn_is_active(self, turn_id: UUID) -> bool:
        async with self._database.sessions() as session:
            state = await session.scalar(
                select(InteractionTurnRecord.state).where(InteractionTurnRecord.id == turn_id)
            )
        return state in {"thinking", "streaming"}

    async def _fail_if_active(self, turn_id: UUID) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            turn = await session.scalar(
                select(InteractionTurnRecord)
                .where(InteractionTurnRecord.id == turn_id)
                .with_for_update()
            )
            if turn is not None and turn.state in {"accepted", "thinking", "streaming"}:
                turn.state = "failed"
                turn.state_version += 1
                turn.completed_at = now

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
