"""SQL implementation of owned operational facts; no ORM objects escape."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select

from app.context.repository import attach_memory_lineage
from app.db import (
    AppUserRecord,
    CognitiveDecisionRecord,
    CognitiveFeedbackRecord,
    ConversationRecord,
    Database,
    DeviceClientRecord,
    MessageRecord,
)
from app.harness.context import ContextReference
from app.schemas import PrivacyLevel
from app.schemas.common import persistent_privacy_levels

from .models import FeedbackKind
from .world_facts import WorldFacts


class SqlWorldFactsRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def read(
        self, *, user_id: UUID, trigger_kind: str, privacy_level: PrivacyLevel, now: datetime
    ) -> WorldFacts:
        since = now - timedelta(hours=4)
        online_since = now - timedelta(seconds=90)
        visible_levels = persistent_privacy_levels(privacy_level)
        async with self._database.sessions() as session:
            timezone = await session.scalar(
                select(AppUserRecord.timezone).where(AppUserRecord.id == user_id)
            )
            last_interaction = await session.scalar(
                select(func.max(MessageRecord.created_at))
                .join(ConversationRecord, ConversationRecord.id == MessageRecord.conversation_id)
                .where(
                    ConversationRecord.user_id == user_id,
                    MessageRecord.privacy_level.in_(visible_levels),
                )
            )
            devices = list(
                await session.scalars(
                    select(DeviceClientRecord).where(
                        DeviceClientRecord.owner_user_id == user_id,
                        DeviceClientRecord.revoked_at.is_(None),
                        DeviceClientRecord.last_seen_at >= online_since,
                    )
                )
            )
            recent_count = int(
                await session.scalar(
                    select(func.count(CognitiveDecisionRecord.id)).where(
                        CognitiveDecisionRecord.user_id == user_id,
                        CognitiveDecisionRecord.created_at >= now - timedelta(days=1),
                        CognitiveDecisionRecord.decision.in_(
                            ["inform", "ask", "suggest", "escalate"]
                        ),
                    )
                )
                or 0
            )
            # 重复打扰惩罚只统计真正浮出水面的决策（record 及以上）；
            # 高频事件源（如屏幕感知）的 below-threshold 静默判定不应累积惩罚，
            # 否则活跃使用几分钟内通道就被结构性压死。同内容去重由上游
            # dedupe_key 与感知管线窗口负责。
            same_count = int(
                await session.scalar(
                    select(func.count(CognitiveDecisionRecord.id)).where(
                        CognitiveDecisionRecord.user_id == user_id,
                        CognitiveDecisionRecord.trigger_kind == trigger_kind,
                        CognitiveDecisionRecord.decision != "ignore",
                        CognitiveDecisionRecord.created_at >= since,
                    )
                )
                or 0
            )
            ignored_count = int(
                await session.scalar(
                    select(func.count(CognitiveFeedbackRecord.id))
                    .join(
                        CognitiveDecisionRecord,
                        CognitiveDecisionRecord.id == CognitiveFeedbackRecord.decision_id,
                    )
                    .where(
                        CognitiveFeedbackRecord.user_id == user_id,
                        CognitiveDecisionRecord.trigger_kind == trigger_kind,
                        CognitiveDecisionRecord.user_id == user_id,
                        CognitiveFeedbackRecord.kind.in_(
                            [FeedbackKind.IGNORED.value, FeedbackKind.FORBIDDEN.value]
                        ),
                    )
                )
                or 0
            )
        capabilities = sorted(
            {
                capability
                for device in devices
                for capability in set(device.capabilities) & set(device.granted_capabilities)
            }
        )
        return WorldFacts(
            timezone=timezone,
            last_interaction_at=last_interaction,
            active_capabilities=tuple(capabilities),
            recent_proactive_count=recent_count,
            same_trigger_recent_count=same_count,
            ignored_same_trigger_count=ignored_count,
        )

    async def attach_memory_lineage(
        self, references: tuple[ContextReference, ...], *, user_id: UUID
    ) -> tuple[ContextReference, ...]:
        async with self._database.sessions() as session:
            return await attach_memory_lineage(session, references, owner_id=user_id)
