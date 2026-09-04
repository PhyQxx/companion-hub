from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from app.chat.service import PendingTurn
from app.db import Database, InteractionTurnRecord, UserModeRecord
from app.runtime.lease import LeaseManager, LeaseResult
from app.schemas import PrivacyLevel
from app.tools import ClientLocation

logger = logging.getLogger("app.runtime.turns")

TurnState = Literal[
    "accepted",
    "listening",
    "thinking",
    "streaming",
    "speaking",
    "interrupted",
    "cancelled",
    "failed",
    "completed",
]

# 合法状态转移图 (source -> allowed targets)
_TRANSITIONS: dict[TurnState, set[TurnState]] = {
    "accepted": {"listening", "thinking", "cancelled"},
    "listening": {"thinking", "cancelled"},
    "thinking": {"streaming", "cancelled", "failed"},
    "streaming": {"speaking", "completed", "interrupted", "cancelled"},
    "speaking": {"completed", "interrupted", "cancelled"},
    "interrupted": {"cancelled"},
    "cancelled": set(),
    "failed": {"completed"},
    "completed": set(),
}

# 可被新输入自动中断的状态
_INTERRUPTIBLE: set[TurnState] = {"thinking", "streaming", "speaking"}


@dataclass(frozen=True, slots=True)
class TurnContext:
    turn_id: UUID
    generation_id: UUID
    conversation_id: UUID
    user_id: UUID
    turn_seq: int
    state: TurnState
    state_version: int


class ChatServiceLike(Protocol):
    """ChatService 的最小协议，TurnCoordinator 依赖其创建/取消回合。"""

    async def start_turn(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
        max_context_messages: int | None = None,
        client_location: ClientLocation | None = None,
    ) -> PendingTurn: ...

    async def cancel_turn(
        self,
        generation_id: UUID,
        *,
        user_id: UUID,
        reason: str,
    ) -> bool: ...


class TurnCoordinator:
    """交互回合编排边界。

    职责：
    - 创建 turn / generation / cancel token；
    - 维护状态转移并验证合法性（CAS）；
    - 选择音频输入和输出终端（委托 LeaseManager）；
    - 向检索、工具、LLM、TTS 传播取消；
    - 拒绝迟到的流式块和动作；
    - 记录降级原因与最终完成状态；
    - 重启后从事件重建未完成回合，并将不安全续跑的回合标记为 cancelled。
    """

    def __init__(
        self,
        database: Database,
        chat_service: ChatServiceLike,
        lease_manager: LeaseManager | None = None,
    ) -> None:
        self._database = database
        self._chat = chat_service
        self._leases = lease_manager or LeaseManager(database)

    # ------------------------------------------------------------------ #
    # 回合生命周期
    # ------------------------------------------------------------------ #

    async def create_turn(
        self,
        *,
        conversation_id: UUID,
        user_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
        mode: Literal["text", "voice"],
        max_context_messages: int | None = None,
        client_location: ClientLocation | None = None,
    ) -> TurnContext:
        """创建新回合。

        自动使同会话中旧的可中断 generation 失效（优雅取消）。
        """
        # 1. 取消同会话中旧的可中断回合
        await self._cancel_interruptible_in_conversation(
            conversation_id, user_id, reason="new_turn"
        )

        # 2. 通过 ChatService 创建回合记录
        pending = await self._chat.start_turn(
            conversation_id,
            user_id=user_id,
            text=text,
            privacy_level=privacy_level,
            max_context_messages=max_context_messages,
            client_location=client_location,
        )

        # 3. 若 voice 模式，先进入 listening
        turn_id = pending.turn_id
        generation_id = pending.generation_id
        turn_seq = pending.turn_seq

        if mode == "voice":
            await self.transition(turn_id, 1, "listening")

        return TurnContext(
            turn_id=turn_id,
            generation_id=generation_id,
            conversation_id=conversation_id,
            user_id=user_id,
            turn_seq=turn_seq,
            state="listening" if mode == "voice" else "accepted",
            state_version=1 if mode == "text" else 2,
        )

    async def transition(
        self,
        turn_id: UUID,
        expected_version: int,
        target: TurnState,
        *,
        reason: str | None = None,
    ) -> bool:
        """CAS 状态转移。

        只有 expected_version 匹配且当前状态允许转移到 target 时才成功。
        """
        async with self._database.sessions.begin() as session:
            record = await session.scalar(
                select(InteractionTurnRecord).where(
                    InteractionTurnRecord.id == turn_id
                )
            )
            if record is None:
                return False

            current: TurnState = record.state  # type: ignore[assignment]
            if record.state_version != expected_version:
                logger.debug(
                    "turn %s CAS failed: expected v%s, got v%s",
                    turn_id,
                    expected_version,
                    record.state_version,
                )
                return False

            if target not in _TRANSITIONS.get(current, set()):
                logger.warning(
                    "turn %s illegal transition: %s -> %s",
                    turn_id,
                    current,
                    target,
                )
                return False

            values: dict[str, Any] = {
                "state": target,
                "state_version": expected_version + 1,
            }
            if target in ("cancelled", "failed", "completed"):
                values["completed_at"] = datetime.now(UTC)
            if reason is not None and target == "cancelled":
                values["cancel_reason"] = reason

            result = await session.execute(
                update(InteractionTurnRecord)
                .where(
                    InteractionTurnRecord.id == turn_id,
                    InteractionTurnRecord.state_version == expected_version,
                )
                .values(**values)
            )
            return int(cast(CursorResult[Any], result).rowcount or 0) > 0

    async def interrupt(
        self,
        turn_id: UUID,
        *,
        user_id: UUID | None = None,
        reason: str = "barge_in",
    ) -> bool:
        """打断当前回合。

        1. 将回合标记为 interrupted；
        2. 释放相关音频租约；
        3. 通过 ChatService 取消 generation；
        4. 返回是否成功。
        """
        from app.db import ConversationRecord

        async with self._database.sessions.begin() as session:
            record = await session.scalar(
                select(InteractionTurnRecord).where(
                    InteractionTurnRecord.id == turn_id
                )
            )
            if record is None:
                return False
            if record.state not in _INTERRUPTIBLE:
                return False

            # 如果未传入 user_id，从 conversation 反查
            if user_id is None:
                conv = await session.scalar(
                    select(ConversationRecord).where(
                        ConversationRecord.id == record.conversation_id
                    )
                )
                user_id = conv.user_id if conv is not None else None

            # CAS 到 interrupted
            result = await session.execute(
                update(InteractionTurnRecord)
                .where(
                    InteractionTurnRecord.id == turn_id,
                    InteractionTurnRecord.state.in_(_INTERRUPTIBLE),
                )
                .values(
                    state="interrupted",
                    state_version=InteractionTurnRecord.state_version + 1,
                )
            )
            if int(cast(CursorResult[Any], result).rowcount or 0) == 0:
                return False

        # 异步释放租约（不阻塞状态转移事务）。租约持有者是连接级 device_id，
        # 中断路径只掌握 generation，按 generation 释放。
        with contextlib.suppress(Exception):
            await self._leases.release_for_generation("audio_output", record.generation_id)

        # 通过 ChatService 传播取消
        if user_id is not None:
            try:
                await self._chat.cancel_turn(
                    record.generation_id,
                    user_id=user_id,
                    reason=reason,
                )
            except Exception:
                logger.exception("cancel_turn failed during interrupt for %s", turn_id)

        return True

    # ------------------------------------------------------------------ #
    # Generation 活性检查
    # ------------------------------------------------------------------ #

    async def is_generation_active(self, generation_id: UUID) -> bool:
        """检查 generation 是否仍活跃（未被取消/打断/完成）。"""
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(InteractionTurnRecord).where(
                    InteractionTurnRecord.generation_id == generation_id
                )
            )
            if record is None:
                return False
            return record.state in {"accepted", "listening", "thinking", "streaming", "speaking"}

    async def current_active_generation(
        self,
        conversation_id: UUID,
    ) -> UUID | None:
        """获取指定会话当前最新的活跃 generation_id。"""
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(InteractionTurnRecord)
                .where(
                    InteractionTurnRecord.conversation_id == conversation_id,
                    InteractionTurnRecord.state.in_(
                        {"accepted", "listening", "thinking", "streaming", "speaking"}
                    ),
                )
                .order_by(InteractionTurnRecord.turn_seq.desc())
                .limit(1)
            )
            return record.generation_id if record else None

    async def reject_stale(
        self,
        generation_id: UUID,
        *,
        payload_kind: str = "delta",
    ) -> bool:
        """拒绝迟到的流式块/动作。若 generation 已不活跃则记录 stale_generation。"""
        if await self.is_generation_active(generation_id):
            return False
        logger.info(
            "stale_generation rejected: %s kind=%s",
            generation_id,
            payload_kind,
        )
        return True

    # ------------------------------------------------------------------ #
    # 租约委托
    # ------------------------------------------------------------------ #

    async def acquire_audio_lease(
        self,
        device_id: UUID,
        generation_id: UUID,
        *,
        ttl_seconds: float = 30.0,
    ) -> LeaseResult:
        return await self._leases.acquire(
            "audio_output",
            device_id,
            generation_id=generation_id,
            ttl_seconds=ttl_seconds,
        )

    async def acquire_microphone(
        self,
        device_id: UUID,
        *,
        ttl_seconds: float = 30.0,
    ) -> LeaseResult:
        return await self._leases.acquire(
            "microphone",
            device_id,
            ttl_seconds=ttl_seconds,
        )

    async def renew_audio_lease(
        self,
        device_id: UUID,
        *,
        ttl_seconds: float = 30.0,
    ) -> LeaseResult:
        return await self._leases.renew(
            "audio_output",
            device_id,
            ttl_seconds=ttl_seconds,
        )

    async def release_audio_lease(self, device_id: UUID) -> bool:
        return await self._leases.release("audio_output", device_id)

    async def expire_stale_leases(self) -> int:
        """清理已过期的音频/麦克风租约；启动时调用兜底崩溃遗留。"""
        expired = await self._leases.expire_stale()
        return len(expired)

    async def current_audio_holder(self) -> UUID | None:
        """当前 audio_output 租约持有者；无人持有时返回 None。"""
        result = await self._leases.current_holder("audio_output")
        return result.holder_device_id if result is not None else None

    async def release_audio_lease_for_generation(self, generation_id: UUID) -> bool:
        return await self._leases.release_for_generation("audio_output", generation_id)

    async def release_microphone(self, device_id: UUID) -> bool:
        return await self._leases.release("microphone", device_id)

    # ------------------------------------------------------------------ #
    # 用户模式
    # ------------------------------------------------------------------ #

    async def set_user_mode(
        self,
        user_id: UUID,
        mode: Literal["available", "focus", "dnd", "asleep", "away"],
        *,
        source: str,
        reason: str | None = None,
        confidence: float | None = None,
        priority: int = 50,
        expires_at: datetime | None = None,
    ) -> None:
        """设置用户模式，自动 supersede 同用户低优先级模式。"""
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            # 标记同用户旧的未过期模式为 superseded
            await session.execute(
                update(UserModeRecord)
                .where(
                    UserModeRecord.user_id == user_id,
                    UserModeRecord.superseded_at.is_(None),
                    UserModeRecord.priority < priority,
                )
                .values(superseded_at=now)
            )
            session.add(
                UserModeRecord(
                    user_id=user_id,
                    mode=mode,
                    source=source,
                    reason=reason,
                    confidence=confidence,
                    priority=priority,
                    starts_at=now,
                    expires_at=expires_at,
                )
            )

    async def current_user_mode(
        self,
        user_id: UUID,
    ) -> str | None:
        """获取当前有效用户模式（按优先级最高、未过期、未被取代）。"""
        now = datetime.now(UTC)
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(UserModeRecord)
                .where(
                    UserModeRecord.user_id == user_id,
                    UserModeRecord.superseded_at.is_(None),
                    (
                        (UserModeRecord.expires_at.is_(None))
                        | (UserModeRecord.expires_at > now)
                    ),
                )
                .order_by(UserModeRecord.priority.desc(), UserModeRecord.starts_at.desc())
                .limit(1)
            )
            return record.mode if record else None

    # ------------------------------------------------------------------ #
    # 重启恢复
    # ------------------------------------------------------------------ #

    async def recover_after_restart(self) -> int:
        """重启后将不安全的未完成回合标记为 cancelled。

        安全状态：completed / cancelled / failed（已终态）
        不安全状态：accepted / listening / thinking / streaming / speaking / interrupted

        返回被清理的回合数量。
        """
        unsafe_states = {
            "accepted",
            "listening",
            "thinking",
            "streaming",
            "speaking",
            "interrupted",
        }
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(InteractionTurnRecord)
                .where(
                    InteractionTurnRecord.state.in_(unsafe_states),
                )
                .values(
                    state="cancelled",
                    cancel_reason="restart_recovery",
                    completed_at=now,
                    state_version=InteractionTurnRecord.state_version + 1,
                )
            )
            count = int(cast(CursorResult[Any], result).rowcount or 0)
            if count:
                logger.warning(
                    "recovered %s unfinished turn(s) to cancelled after restart",
                    count,
                )
            return count

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    async def _cancel_interruptible_in_conversation(
        self,
        conversation_id: UUID,
        user_id: UUID,
        *,
        reason: str,
    ) -> None:
        """取消同会话中所有可中断的旧回合。"""
        async with self._database.sessions() as session:
            rows = await session.scalars(
                select(InteractionTurnRecord).where(
                    InteractionTurnRecord.conversation_id == conversation_id,
                    InteractionTurnRecord.state.in_(_INTERRUPTIBLE),
                )
            )
            for record in rows.all():
                try:
                    await self._chat.cancel_turn(
                        record.generation_id,
                        user_id=user_id,
                        reason=reason,
                    )
                except Exception:
                    logger.exception(
                        "failed to cancel old generation %s",
                        record.generation_id,
                    )
