"""SQL adapter for output ownership and independent delivery audit."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.context.owners import observation_owner
from app.db import AppUserRecord, Database, ProactiveDeliveryReceiptRecord
from app.ids import uuid7
from app.schemas import PrivacyLevel

from .contracts import ProactiveChannelAttempt


class SqlProactiveOutputRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def default_owner(self) -> UUID | None:
        # 隐式主动输出来源绑定单一 owner（slot=1），停用即拒绝投递，
        # 不静默改投他人；绑定落定在 observation_owner 内业主优先。
        return await observation_owner(self._database)

    async def owner_active(self, user_id: UUID) -> bool:
        async with self._database.sessions() as session:
            owner = await session.scalar(
                select(AppUserRecord.id).where(
                    AppUserRecord.id == user_id, AppUserRecord.status == "active"
                )
            )
        return owner == user_id

    async def record_attempts(
        self,
        user_id: UUID,
        attempts: tuple[ProactiveChannelAttempt, ...],
        *,
        privacy_level: PrivacyLevel,
        decision_id: UUID | None,
    ) -> None:
        if not attempts or privacy_level == PrivacyLevel.L3:
            return
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            session.add_all(
                ProactiveDeliveryReceiptRecord(
                    id=uuid7(),
                    user_id=user_id,
                    decision_id=decision_id,
                    channel=attempt.channel,
                    status="delivered" if attempt.delivered else "failed",
                    reason_code=attempt.reason_code,
                    external_operation_id=attempt.external_operation_id,
                    privacy_level=str(privacy_level),
                    created_at=now,
                )
                for attempt in attempts
            )
