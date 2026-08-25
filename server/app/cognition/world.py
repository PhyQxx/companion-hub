from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db import (
    AppUserRecord,
    CognitiveDecisionRecord,
    CognitiveFeedbackRecord,
    ConversationRecord,
    Database,
    DeviceClientRecord,
    MessageRecord,
)
from app.memory import MemoryRetriever
from app.schemas import PrivacyLevel
from app.timeline import TimelineStore

from .models import FeedbackKind, SemanticEvent, WorldState
from .store import CognitiveStore


class WorldStateBuilder:
    def __init__(
        self,
        database: Database,
        store: CognitiveStore,
        *,
        memory_retriever: MemoryRetriever | None = None,
        timeline_store: TimelineStore | None = None,
    ) -> None:
        self._database = database
        self._store = store
        self._memory_retriever = memory_retriever
        self._timeline_store = timeline_store

    async def build(self, event: SemanticEvent, *, now: datetime | None = None) -> WorldState:
        moment = now or datetime.now(UTC)
        since = moment - timedelta(hours=4)
        online_since = moment - timedelta(seconds=90)
        async with self._database.sessions() as session:
            timezone = await session.scalar(
                select(AppUserRecord.timezone).where(AppUserRecord.id == event.user_id)
            )
            last_interaction = await session.scalar(
                select(func.max(MessageRecord.created_at))
                .join(ConversationRecord, ConversationRecord.id == MessageRecord.conversation_id)
                .where(ConversationRecord.user_id == event.user_id)
            )
            devices = list(
                await session.scalars(
                    select(DeviceClientRecord).where(
                        DeviceClientRecord.owner_user_id == event.user_id,
                        DeviceClientRecord.revoked_at.is_(None),
                        DeviceClientRecord.last_seen_at >= online_since,
                    )
                )
            )
            recent_count = int(
                await session.scalar(
                    select(func.count(CognitiveDecisionRecord.id)).where(
                        CognitiveDecisionRecord.user_id == event.user_id,
                        CognitiveDecisionRecord.created_at >= moment - timedelta(days=1),
                        CognitiveDecisionRecord.decision.in_(
                            ["inform", "ask", "suggest", "escalate"]
                        ),
                    )
                )
                or 0
            )
            same_count = int(
                await session.scalar(
                    select(func.count(CognitiveDecisionRecord.id)).where(
                        CognitiveDecisionRecord.user_id == event.user_id,
                        CognitiveDecisionRecord.trigger_kind == event.kind,
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
                        CognitiveFeedbackRecord.user_id == event.user_id,
                        CognitiveDecisionRecord.trigger_kind == event.kind,
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
        memory_ids: list[str] = []
        if self._memory_retriever is not None and event.privacy_level != PrivacyLevel.L3:
            try:
                memory_result = await self._memory_retriever.retrieve(
                    event.summary,
                    user_id=event.user_id,
                    privacy_level=event.privacy_level,
                    now=moment,
                )
                memory_ids = [str(hit.memory.id) for hit in memory_result.hits[:4]]
            except Exception:
                memory_ids = []
        timeline_ids: list[str] = []
        if self._timeline_store is not None and event.privacy_level != PrivacyLevel.L3:
            try:
                timeline_result = await self._timeline_store.search(
                    user_id=event.user_id,
                    query=event.summary,
                    privacy_levels=(
                        (PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2)
                        if event.privacy_level == PrivacyLevel.L2
                        else (PrivacyLevel.L0, PrivacyLevel.L1)
                    ),
                    limit=4,
                )
                timeline_ids = [str(item.id) for item in timeline_result.events]
            except Exception:
                timeline_ids = []
        return WorldState(
            built_at=moment,
            timezone=timezone or "Asia/Shanghai",
            last_interaction_at=last_interaction,
            active_capabilities=capabilities,
            active_goals=await self._store.active_goals(event.user_id, now=moment),
            memory_evidence_ids=memory_ids,
            timeline_evidence_ids=timeline_ids,
            recent_proactive_count=recent_count,
            same_trigger_recent_count=same_count,
            ignored_same_trigger_count=ignored_count,
            dnd=bool(event.attributes.get("dnd", False)),
        )
