"""SAFE-02 告警状态机的持久化层。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.db import (
    Database,
    SafetyAlertEscalationRecord,
    SafetyAlertRecord,
    SafetyAuthorizationRecord,
)
from app.ids import uuid7


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class SafetyAlertStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(
        self,
        *,
        user_id: UUID,
        rule_id: str,
        entity_id: str,
        message: str,
        evidence: dict[str, object],
        l1_at: datetime,
        expires_at: datetime,
    ) -> SafetyAlertRecord:
        record = SafetyAlertRecord(
            id=uuid7(),
            user_id=user_id,
            rule_id=rule_id,
            entity_id=entity_id,
            severity="critical",
            message=message[:2_000],
            evidence=dict(evidence),
            status="escalating",
            level=1,
            l1_at=_aware(l1_at),
            expires_at=_aware(expires_at),
        )
        async with self._database.sessions.begin() as session:
            session.add(record)
        return record

    async def get(self, alert_id: UUID) -> SafetyAlertRecord | None:
        async with self._database.sessions() as session:
            record: SafetyAlertRecord | None = await session.get(SafetyAlertRecord, alert_id)
            return record

    async def active_for(
        self, *, user_id: UUID, entity_id: str, rule_id: str
    ) -> SafetyAlertRecord | None:
        async with self._database.sessions() as session:
            found: SafetyAlertRecord | None = await session.scalar(
                select(SafetyAlertRecord)
                .where(
                    SafetyAlertRecord.user_id == user_id,
                    SafetyAlertRecord.entity_id == entity_id,
                    SafetyAlertRecord.rule_id == rule_id,
                    SafetyAlertRecord.status == "escalating",
                )
                .order_by(SafetyAlertRecord.created_at.desc())
                .limit(1)
            )
            return found

    async def escalating_for_user(self, user_id: UUID) -> list[SafetyAlertRecord]:
        async with self._database.sessions() as session:
            return list(
                await session.scalars(
                    select(SafetyAlertRecord)
                    .where(
                        SafetyAlertRecord.user_id == user_id,
                        SafetyAlertRecord.status == "escalating",
                    )
                    .order_by(SafetyAlertRecord.created_at.desc())
                )
            )

    async def all_escalating(self) -> list[SafetyAlertRecord]:
        async with self._database.sessions() as session:
            found_all: list[SafetyAlertRecord] = list(
                await session.scalars(
                    select(SafetyAlertRecord).where(
                        SafetyAlertRecord.status == "escalating"
                    )
                )
            )
            return found_all

    async def mark_escalated(
        self, alert_id: UUID, *, level: int, at: datetime
    ) -> SafetyAlertRecord | None:
        async with self._database.sessions.begin() as session:
            record = await session.get(SafetyAlertRecord, alert_id)
            if record is None or record.status != "escalating":
                return record
            record.level = level
            if level >= 2:
                record.l2_at = _aware(at)
            await session.flush()
            refreshed = await session.get(SafetyAlertRecord, alert_id)
            assert refreshed is not None
            return refreshed

    async def mark_acked(
        self, alert_id: UUID, *, at: datetime, source: str
    ) -> SafetyAlertRecord | None:
        async with self._database.sessions.begin() as session:
            record = await session.get(SafetyAlertRecord, alert_id)
            if record is None or record.status != "escalating":
                return record
            record.status = "acknowledged"
            record.acked_at = _aware(at)
            record.ack_source = source[:32]
            return record

    async def mark_expired(self, alert_id: UUID) -> SafetyAlertRecord | None:
        async with self._database.sessions.begin() as session:
            record = await session.get(SafetyAlertRecord, alert_id)
            if record is None or record.status != "escalating":
                return record
            record.status = "expired"
            return record


class SafetyAuthorizationStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(
        self, *, user_id: UUID, contact_name: str, destination: str
    ) -> SafetyAuthorizationRecord:
        record = SafetyAuthorizationRecord(
            id=uuid7(),
            user_id=user_id,
            contact_name=contact_name[:120],
            channel="email",
            destination=destination[:254],
            status="active",
        )
        async with self._database.sessions.begin() as session:
            session.add(record)
        return record

    async def list_for_user(
        self, user_id: UUID, *, status: str | None = None
    ) -> list[SafetyAuthorizationRecord]:
        query = select(SafetyAuthorizationRecord).where(
            SafetyAuthorizationRecord.user_id == user_id
        )
        if status is not None:
            query = query.where(SafetyAuthorizationRecord.status == status)
        async with self._database.sessions() as session:
            found: list[SafetyAuthorizationRecord] = list(
                await session.scalars(query.order_by(SafetyAuthorizationRecord.created_at))
            )
            return found

    async def active_for_user(self, user_id: UUID) -> SafetyAuthorizationRecord | None:
        records = await self.list_for_user(user_id, status="active")
        return records[0] if records else None

    async def revoke(
        self, authorization_id: UUID, *, at: datetime
    ) -> SafetyAuthorizationRecord | None:
        async with self._database.sessions.begin() as session:
            record = await session.get(SafetyAuthorizationRecord, authorization_id)
            if record is None or record.status != "active":
                return record
            record.status = "revoked"
            record.revoked_at = _aware(at)
            return record


class SafetyEscalationLedger:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def record(
        self,
        *,
        alert_id: UUID,
        user_id: UUID,
        channel: str,
        destination: str,
        status: str,
        reason: str | None = None,
    ) -> SafetyAlertEscalationRecord:
        record = SafetyAlertEscalationRecord(
            id=uuid7(),
            alert_id=alert_id,
            user_id=user_id,
            level=3,
            channel=channel[:16],
            destination=destination[:254],
            status=status[:16],
            reason=reason[:160] if reason else None,
        )
        async with self._database.sessions.begin() as session:
            session.add(record)
        return record

    async def contacted(self, alert_id: UUID) -> bool:
        async with self._database.sessions() as session:
            found = await session.scalar(
                select(SafetyAlertEscalationRecord.id)
                .where(
                    SafetyAlertEscalationRecord.alert_id == alert_id,
                    SafetyAlertEscalationRecord.level == 3,
                )
                .limit(1)
            )
            return found is not None
