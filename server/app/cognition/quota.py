"""No-content quota entries plus legacy decisions that have no retained entry."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import CognitiveDecisionRecord, ProactiveQuotaEntryRecord

from .proactive import VISIBLE_DECISIONS


async def proactive_count(
    session: AsyncSession, user_id: UUID, start: datetime, end: datetime
) -> int:
    retained = await session.scalar(
        select(func.count())
        .select_from(ProactiveQuotaEntryRecord)
        .where(
            ProactiveQuotaEntryRecord.user_id == user_id,
            ProactiveQuotaEntryRecord.accepted_at >= start,
            ProactiveQuotaEntryRecord.accepted_at < end,
        )
    )
    legacy = await session.scalar(
        select(func.count())
        .select_from(CognitiveDecisionRecord)
        .where(
            CognitiveDecisionRecord.user_id == user_id,
            CognitiveDecisionRecord.created_at >= start,
            CognitiveDecisionRecord.created_at < end,
            CognitiveDecisionRecord.decision.in_(VISIBLE_DECISIONS),
            ~exists().where(ProactiveQuotaEntryRecord.decision_id == CognitiveDecisionRecord.id),
        )
    )
    return int(retained or 0) + int(legacy or 0)
