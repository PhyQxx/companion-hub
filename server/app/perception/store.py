from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update

from app.cognition import SemanticEvent
from app.db import AppUserRecord, Database, SemanticEventAuditRecord
from app.db.claims import assert_current_claim
from app.harness.budget import BudgetDenied
from app.schemas import PrivacyLevel

from .models import PerceptionDisposition, SemanticEventAuditView
from .ports import EventAuditSnapshot


def _snapshot(record: SemanticEventAuditRecord | None) -> EventAuditSnapshot | None:
    if record is None:
        return None
    return EventAuditSnapshot(
        event_id=record.event_id,
        user_id=record.user_id,
        kind=record.kind,
        source_kind=record.source_kind,
        dedupe_key=record.dedupe_key,
        privacy_level=record.privacy_level,
        evidence_ids=tuple(record.evidence_ids),
        occurred_at=record.occurred_at,
        expires_at=record.expires_at,
    )


class PerceptionStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get(self, event_id: UUID) -> EventAuditSnapshot | None:
        async with self.database.sessions() as session:
            record = await session.get(SemanticEventAuditRecord, event_id)
            return _snapshot(record)

    async def recent_duplicate(
        self,
        event: SemanticEvent,
        *,
        dedupe_key: str,
        now: datetime,
        window_seconds: int,
    ) -> EventAuditSnapshot | None:
        async with self.database.sessions() as session:
            record = await session.scalar(
                select(SemanticEventAuditRecord)
                .where(
                    SemanticEventAuditRecord.user_id == event.user_id,
                    SemanticEventAuditRecord.dedupe_key == dedupe_key,
                    SemanticEventAuditRecord.disposition.in_(["processed", "suppressed"]),
                    SemanticEventAuditRecord.created_at >= now - timedelta(seconds=window_seconds),
                )
                .order_by(SemanticEventAuditRecord.created_at.desc())
                .limit(1)
            )
            return _snapshot(record)

    async def record(
        self,
        event: SemanticEvent,
        *,
        dedupe_key: str,
        disposition: PerceptionDisposition,
        now: datetime,
        reason_code: str | None = None,
        decision_id: UUID | None = None,
        merged_into_event_id: UUID | None = None,
    ) -> None:
        event = event.model_copy(deep=True)
        if event.privacy_level == PrivacyLevel.L3:
            return
        async with self.database.sessions.begin() as session:
            await assert_current_claim(session)
            owner = await session.scalar(
                update(AppUserRecord)
                .where(AppUserRecord.id == event.user_id, AppUserRecord.status == "active")
                .values(status=AppUserRecord.status)
                .returning(AppUserRecord.id)
                .execution_options(synchronize_session=False)
            )
            if owner is None:
                raise BudgetDenied("event_owner_inactive")
            session.add(
                SemanticEventAuditRecord(
                    event_id=event.event_id,
                    user_id=event.user_id,
                    kind=event.kind,
                    source_kind=event.source_kind,
                    dedupe_key=dedupe_key,
                    privacy_level=str(event.privacy_level),
                    evidence_ids=event.evidence_ids,
                    disposition=str(disposition),
                    reason_code=reason_code,
                    decision_id=decision_id,
                    merged_into_event_id=merged_into_event_id,
                    occurred_at=event.occurred_at,
                    expires_at=event.expires_at,
                    created_at=now,
                )
            )

    async def recent(self, user_id: UUID, *, limit: int = 100) -> list[SemanticEventAuditView]:
        async with self.database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(SemanticEventAuditRecord)
                    .where(SemanticEventAuditRecord.user_id == user_id)
                    .order_by(SemanticEventAuditRecord.created_at.desc())
                    .limit(limit)
                )
            )
        return [
            SemanticEventAuditView(
                event_id=row.event_id,
                kind=row.kind,
                source_kind=row.source_kind,
                disposition=row.disposition,
                reason_code=row.reason_code,
                decision_id=row.decision_id,
                merged_into_event_id=row.merged_into_event_id,
                evidence_ids=row.evidence_ids,
                occurred_at=row.occurred_at,
                expires_at=row.expires_at,
                created_at=row.created_at,
            )
            for row in rows
        ]
