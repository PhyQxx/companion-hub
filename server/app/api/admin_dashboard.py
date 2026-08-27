from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.db import (
    AppUserRecord,
    AuthSessionRecord,
    CognitiveDecisionRecord,
    ConfigVersionRecord,
    ConversationRecord,
    Database,
    DeadLetterRecord,
    DeletionLedgerRecord,
    DeviceClientRecord,
    DeviceCommandRecord,
    EventRecord,
    MemoryRecord,
    MessageRecord,
    OutboxRecord,
    ProactiveDeliveryReceiptRecord,
    SemanticEventAuditRecord,
    TimelineEventRecord,
)
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard


class DecisionTraceView(StrictModel):
    id: str
    user_id: str
    trigger_kind: str
    decision: str
    confidence: float
    urgency: str
    attention_score: float
    model_name: str | None
    created_at: datetime


class EventAuditView(StrictModel):
    event_id: str
    user_id: str
    kind: str
    source_kind: str
    disposition: str
    privacy_level: str
    occurred_at: datetime


class DeadLetterView(StrictModel):
    id: int
    topic: str
    attempts: int
    error_code: str
    created_at: datetime


class CommandErrorView(StrictModel):
    id: str
    device_id: str
    command: str
    status: str
    reason_code: str | None
    issued_at: datetime


class LogsDashboardView(StrictModel):
    traces: list[DecisionTraceView]
    events: list[EventAuditView]
    dead_letters: list[DeadLetterView]
    command_errors: list[CommandErrorView]
    trace_count: int
    event_count: int
    dead_letter_count: int
    command_error_count: int


class PrivacyDistributionView(StrictModel):
    level: str
    event_count: int
    message_count: int
    memory_count: int
    timeline_count: int


class EgressRecordView(StrictModel):
    id: str
    kind: str
    provider: str | None
    privacy_level: str
    created_at: datetime


class OperationAuditView(StrictModel):
    id: str
    kind: str
    actor: str
    description: str
    created_at: datetime


class PrivacyDashboardView(StrictModel):
    summary: list[PrivacyDistributionView]
    egress: list[EgressRecordView]
    operations: list[OperationAuditView]
    total_conversations: int
    total_messages: int
    total_memories: int


class UserIdentityView(StrictModel):
    id: str
    display_name: str
    status: str
    created_at: datetime
    session_count: int


class StorageTableView(StrictModel):
    name: str
    row_count: int


class SystemDashboardView(StrictModel):
    version: str
    users: list[UserIdentityView]
    active_sessions: int
    total_conversations: int
    storage: list[StorageTableView]
    database_url_type: str


class UsageDashboardView(StrictModel):
    total_conversations: int
    active_conversations: int
    total_messages: int
    messages_by_role: dict[str, int]
    messages_by_privacy_level: dict[str, int]


def create_admin_dashboard_router(
    database: Database,
    *,
    admin_token: str | None,
    version: str,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/dashboard",
        tags=["admin-dashboard"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("/logs", response_model=LogsDashboardView)
    async def logs_dashboard(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> LogsDashboardView:
        async with database.sessions() as session:
            # Traces: cognitive decisions
            decision_rows = await session.execute(
                select(CognitiveDecisionRecord).order_by(CognitiveDecisionRecord.created_at.desc()).limit(limit)
            )
            decisions = decision_rows.scalars().all()

            # Events: semantic event audit
            event_rows = await session.execute(
                select(SemanticEventAuditRecord).order_by(SemanticEventAuditRecord.created_at.desc()).limit(limit)
            )
            events = event_rows.scalars().all()

            # Dead letters
            dl_rows = await session.execute(
                select(DeadLetterRecord).order_by(DeadLetterRecord.created_at.desc()).limit(limit)
            )
            dead_letters = dl_rows.scalars().all()

            # Command errors
            cmd_rows = await session.execute(
                select(DeviceCommandRecord)
                .where(DeviceCommandRecord.status.in_(["failed", "timed_out", "expired"]))
                .order_by(DeviceCommandRecord.issued_at.desc())
                .limit(limit)
            )
            command_errors = cmd_rows.scalars().all()

            # Counts
            trace_count = await session.scalar(select(func.count(CognitiveDecisionRecord.id)))
            event_count = await session.scalar(
                select(func.count(SemanticEventAuditRecord.event_id))
            )
            dl_count = await session.scalar(select(func.count(DeadLetterRecord.id)))
            cmd_err_count = await session.scalar(
                select(func.count(DeviceCommandRecord.id)).where(
                    DeviceCommandRecord.status.in_(["failed", "timed_out", "expired"])
                )
            )

        return LogsDashboardView(
            traces=[
                DecisionTraceView(
                    id=str(d.id),
                    user_id=str(d.user_id),
                    trigger_kind=d.trigger_kind,
                    decision=d.decision,
                    confidence=d.confidence,
                    urgency=d.urgency,
                    attention_score=d.attention_score,
                    model_name=d.model_name,
                    created_at=d.created_at,
                )
                for d in decisions
            ],
            events=[
                EventAuditView(
                    event_id=str(e.event_id),
                    user_id=str(e.user_id),
                    kind=e.kind,
                    source_kind=e.source_kind,
                    disposition=e.disposition,
                    privacy_level=e.privacy_level,
                    occurred_at=e.occurred_at,
                )
                for e in events
            ],
            dead_letters=[
                DeadLetterView(
                    id=dl.id,
                    topic=dl.topic,
                    attempts=dl.attempts,
                    error_code=dl.error_code,
                    created_at=dl.created_at,
                )
                for dl in dead_letters
            ],
            command_errors=[
                CommandErrorView(
                    id=str(c.id),
                    device_id=str(c.device_id),
                    command=c.command_name,
                    status=c.status,
                    reason_code=c.reason_code,
                    issued_at=c.issued_at,
                )
                for c in command_errors
            ],
            trace_count=trace_count or 0,
            event_count=event_count or 0,
            dead_letter_count=dl_count or 0,
            command_error_count=cmd_err_count or 0,
        )

    @router.get("/privacy", response_model=PrivacyDashboardView)
    async def privacy_dashboard() -> PrivacyDashboardView:
        async with database.sessions() as session:
            # Privacy level distribution per table
            levels = ["L0", "L1", "L2"]
            summary: list[PrivacyDistributionView] = []
            for level in levels:
                event_count = await session.scalar(
                    select(func.count(EventRecord.event_id)).where(
                        EventRecord.privacy_level == level
                    )
                )
                message_count = await session.scalar(
                    select(func.count(MessageRecord.id)).where(MessageRecord.privacy_level == level)
                )
                memory_count = await session.scalar(
                    select(func.count(MemoryRecord.id)).where(MemoryRecord.privacy_level == level)
                )
                timeline_count = await session.scalar(
                    select(func.count(TimelineEventRecord.id)).where(
                        TimelineEventRecord.privacy_level == level
                    )
                )
                summary.append(
                    PrivacyDistributionView(
                        level=level,
                        event_count=event_count or 0,
                        message_count=message_count or 0,
                        memory_count=memory_count or 0,
                        timeline_count=timeline_count or 0,
                    )
                )

            # Egress: proactive delivery receipts + tool ledger is separate
            egress_rows = await session.execute(
                select(ProactiveDeliveryReceiptRecord)
                .order_by(ProactiveDeliveryReceiptRecord.created_at.desc())
                .limit(50)
            )
            egress = egress_rows.scalars().all()

            # Operations: config versions + deletion ledger
            ops: list[OperationAuditView] = []
            config_rows = await session.execute(
                select(ConfigVersionRecord).order_by(ConfigVersionRecord.created_at.desc()).limit(25)
            )
            for cv in config_rows.scalars().all():
                ops.append(
                    OperationAuditView(
                        id=str(cv.id),
                        kind="config_version",
                        actor=cv.created_by,
                        description=f"配置版本 v{cv.id} ({cv.status})",
                        created_at=cv.created_at,
                    )
                )
            ledger_rows = await session.execute(
                select(DeletionLedgerRecord).order_by(DeletionLedgerRecord.created_at.desc()).limit(25)
            )
            for dl in ledger_rows.scalars().all():
                ops.append(
                    OperationAuditView(
                        id=str(dl.id),
                        kind="deletion",
                        actor=dl.requested_by or "system",
                        description=f"删除 {dl.entity_kind} ({len(dl.deleted_ids)} 项)",
                        created_at=dl.created_at,
                    )
                )
            ops.sort(key=lambda x: x.created_at, reverse=True)

            total_conversations = await session.scalar(select(func.count(ConversationRecord.id)))
            total_messages = await session.scalar(select(func.count(MessageRecord.id)))
            total_memories = await session.scalar(select(func.count(MemoryRecord.id)))

        return PrivacyDashboardView(
            summary=summary,
            egress=[
                EgressRecordView(
                    id=str(e.id),
                    kind=e.channel,
                    provider=None,
                    privacy_level=e.privacy_level,
                    created_at=e.created_at,
                )
                for e in egress
            ],
            operations=ops[:50],
            total_conversations=total_conversations or 0,
            total_messages=total_messages or 0,
            total_memories=total_memories or 0,
        )

    @router.get("/usage", response_model=UsageDashboardView)
    async def usage_dashboard() -> UsageDashboardView:
        async with database.sessions() as session:
            total_conversations = await session.scalar(
                select(func.count(ConversationRecord.id))
            )
            active_conversations = await session.scalar(
                select(func.count(ConversationRecord.id)).where(
                    ConversationRecord.status == "active"
                )
            )
            total_messages = await session.scalar(select(func.count(MessageRecord.id)))
            role_rows = await session.execute(
                select(MessageRecord.role, func.count(MessageRecord.id)).group_by(
                    MessageRecord.role
                )
            )
            privacy_rows = await session.execute(
                select(MessageRecord.privacy_level, func.count(MessageRecord.id)).group_by(
                    MessageRecord.privacy_level
                )
            )

        return UsageDashboardView(
            total_conversations=total_conversations or 0,
            active_conversations=active_conversations or 0,
            total_messages=total_messages or 0,
            messages_by_role={role: count for role, count in role_rows.all()},
            messages_by_privacy_level={level: count for level, count in privacy_rows.all()},
        )

    @router.get("/system", response_model=SystemDashboardView)
    async def system_dashboard() -> SystemDashboardView:
        async with database.sessions() as session:
            # Users with session counts
            user_rows = await session.execute(
                select(AppUserRecord).order_by(AppUserRecord.created_at.desc()).limit(50)
            )
            users = user_rows.scalars().all()

            user_views: list[UserIdentityView] = []
            for u in users:
                session_count = await session.scalar(
                    select(func.count(AuthSessionRecord.id)).where(
                        AuthSessionRecord.user_id == u.id
                    )
                )
                user_views.append(
                    UserIdentityView(
                        id=str(u.id),
                        display_name=u.display_name,
                        status=u.status,
                        created_at=u.created_at,
                        session_count=session_count or 0,
                    )
                )

            active_sessions = await session.scalar(
                select(func.count(AuthSessionRecord.id)).where(
                    AuthSessionRecord.revoked_at.is_(None),
                    AuthSessionRecord.expires_at > datetime.now(),
                )
            )
            total_conversations = await session.scalar(select(func.count(ConversationRecord.id)))

            # Storage estimates via row counts
            tables = [
                ("event", EventRecord, EventRecord.event_id),
                ("message", MessageRecord, MessageRecord.id),
                ("memory", MemoryRecord, MemoryRecord.id),
                ("conversation", ConversationRecord, ConversationRecord.id),
                ("device_client", DeviceClientRecord, DeviceClientRecord.id),
                ("device_command", DeviceCommandRecord, DeviceCommandRecord.id),
                ("config_version", ConfigVersionRecord, ConfigVersionRecord.id),
                ("timeline_event", TimelineEventRecord, TimelineEventRecord.id),
                ("cognitive_decision", CognitiveDecisionRecord, CognitiveDecisionRecord.id),
                ("outbox", OutboxRecord, OutboxRecord.id),
            ]
            storage: list[StorageTableView] = []
            for name, _model, pk_col in tables:
                count = await session.scalar(select(func.count(pk_col)))
                storage.append(StorageTableView(name=name, row_count=count or 0))

        db_url = str(database.engine.url)
        db_type = (
            "sqlite"
            if "sqlite" in db_url
            else "postgresql"
            if "postgresql" in db_url
            else "other"
        )

        return SystemDashboardView(
            version=version,
            users=user_views,
            active_sessions=active_sessions or 0,
            total_conversations=total_conversations or 0,
            storage=storage,
            database_url_type=db_type,
        )

    return router
