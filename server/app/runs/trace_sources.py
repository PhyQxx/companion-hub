"""SQL authority for a trace whose accepted root explicitly disabled quotas."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AppUserRecord, ConversationRecord, Database, TaskRunRecord
from app.db.claims import assert_current_claim
from app.harness.budget import BudgetDenied
from app.harness.joined_read import joined_read
from app.harness.run_trace import DisabledRunTrace, RunTraceSource
from app.harness.time import utc
from app.harness.voice_sources import VoiceRecipientClaim, VoiceRunFence, VoiceSourceClaim
from app.schemas import PrivacyLevel

from .budget_origins import scope_fingerprint
from .chat_parent import require_chat_parent
from .voice_sources import require_voice_run_fences


def source_fingerprint(row: TaskRunRecord) -> str:
    # Mutable progress, required_work, receipts and usage do not change identity.
    # No prompt, output, credentials or arbitrary contract payload is retained.
    return scope_fingerprint(
        {
            "budget": row.budget,
            "config_version": row.config_version,
            "deadline": utc(row.deadline).isoformat() if row.deadline else None,
            "identity": {
                key: row.contract.get(key)
                for key in (
                    "kind",
                    "entry",
                    "criterion",
                    "input_message_id",
                    "budget_parent_id",
                    "quota_scope",
                    "source_actor",
                    "source_id",
                )
            },
        }
    )


def trace_source(row: TaskRunRecord, *, maintenance: bool) -> RunTraceSource:
    return RunTraceSource(
        row.id,
        row.parent_run_id,
        row.conversation_id,
        PrivacyLevel(row.privacy_level),
        source_fingerprint(row),
        maintenance,
    )


async def require_run_trace(
    session: AsyncSession,
    trace: DisabledRunTrace,
    *,
    user_id: UUID,
    privacy_level: PrivacyLevel,
    lock: bool = False,
) -> None:
    await assert_current_claim(session)
    if trace.user_id != user_id or not trace.sources:
        raise BudgetDenied("budget_owner_invalid")
    if trace.expires_at is not None and utc(trace.expires_at) <= datetime.now(UTC):
        raise BudgetDenied("run_deadline_exceeded")
    owner = await session.get(AppUserRecord, user_id, populate_existing=True)
    if owner is None or owner.status != "active":
        raise BudgetDenied("budget_owner_invalid")
    for ref in trace.sources:
        if lock:
            row = await session.scalar(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == ref.run_id, TaskRunRecord.user_id == user_id)
                .values(updated_at=TaskRunRecord.updated_at)
                .returning(TaskRunRecord)
                .execution_options(synchronize_session=False, populate_existing=True)
            )
        else:
            row = await session.get(TaskRunRecord, ref.run_id, populate_existing=True)
        if (
            row is None
            or row.user_id != user_id
            or row.parent_run_id != ref.parent_run_id
            or row.conversation_id != ref.conversation_id
            or row.privacy_level != str(ref.privacy_level)
            or source_fingerprint(row) != ref.fingerprint
        ):
            raise BudgetDenied("run_trace_changed")
        statuses = (
            {"accepted", "running", "succeeded"} if ref.allow_succeeded else {"accepted", "running"}
        )
        if row.status not in statuses or row.contract.get("work_cancel_requested"):
            raise BudgetDenied("budget_run_inactive")
        if row.contract.get("budget_usage_overflow"):
            raise BudgetDenied("budget_usage_overflow")
        if str(ref.privacy_level) > str(privacy_level):
            raise BudgetDenied("operation_privacy_downgrade")
        # Maintenance may use a completed chat's source after its delivery clock;
        # its accepted plan/job expiry still bounds this trace.
        if (
            row.deadline
            and not (ref.allow_succeeded and row.status == "succeeded")
            and utc(row.deadline) <= datetime.now(UTC)
        ):
            raise BudgetDenied("run_deadline_exceeded")
        if row.conversation_id is not None:
            conversation = await session.get(
                ConversationRecord, row.conversation_id, populate_existing=True
            )
            if (
                conversation is None
                or conversation.user_id != user_id
                or conversation.status != "active"
            ):
                raise BudgetDenied("run_source_not_found")
        if row.contract.get("kind") == "chat.reply" and row.parent_run_id is not None:
            if row.conversation_id is None:
                raise BudgetDenied("chat_parent_changed")
            await require_chat_parent(
                session,
                row.parent_run_id,
                user_id=user_id,
                conversation_id=row.conversation_id,
                privacy_level=ref.privacy_level,
                child_id=row.id,
                allow_succeeded=ref.allow_succeeded,
                quota_scope=row.contract.get("quota_scope"),
            )
        if row.contract.get("entry") == "voice.utterance":
            actor, raw_id = row.contract.get("source_actor"), row.contract.get("source_id")
            try:
                actor_id = UUID(str(raw_id))
            except ValueError as error:
                raise BudgetDenied("voice_actor_invalid") from error
            source: VoiceSourceClaim | VoiceRecipientClaim
            if actor in {"browser", "satellite"}:
                source = VoiceSourceClaim(
                    user_id,
                    row.conversation_id,
                    "browser" if actor == "browser" else "satellite",
                    actor_id,
                    ref.privacy_level,
                )
            elif actor in {"avatar.chat", "voice.satellite"}:
                source = VoiceRecipientClaim(
                    user_id,
                    actor_id,
                    "avatar.chat" if actor == "avatar.chat" else "voice.satellite",
                    ref.privacy_level,
                )
            else:
                raise BudgetDenied("voice_actor_invalid")
            await require_voice_run_fences(
                session,
                source,
                (
                    VoiceRunFence(
                        row.id,
                        bool(row.budget and row.budget.get("enabled")),
                        check_lineage=True,
                        parent_run_id=row.parent_run_id,
                        conversation_id=row.conversation_id,
                        allow_succeeded=ref.allow_succeeded,
                        check_source=True,
                        source_actor=str(actor),
                        source_id=actor_id,
                    ),
                ),
            )


def check_trace_binding(trace: DisabledRunTrace, database: Database, user_id: UUID) -> None:
    if trace.database_key != id(database) or trace.user_id != user_id:
        raise BudgetDenied("budget_owner_invalid")


async def validate_disabled_trace(database: Database, trace: DisabledRunTrace) -> None:
    check_trace_binding(trace, database, trace.user_id)

    async def read() -> None:
        async with database.sessions() as session:
            await require_run_trace(
                session,
                trace,
                user_id=trace.user_id,
                privacy_level=trace.sources[-1].privacy_level,
            )

    await joined_read(read())


async def capture_disabled_trace(
    database: Database,
    run_id: UUID,
    user_id: UUID,
    *,
    maintenance: bool = False,
    expires_at: datetime | None = None,
    require_disabled: bool = False,
) -> DisabledRunTrace | None:
    async def read() -> DisabledRunTrace | None:
        async with database.sessions() as session:
            rows: list[TaskRunRecord] = []
            target: UUID | None = run_id
            seen: set[UUID] = set()
            while target is not None:
                if target in seen or len(seen) >= 32:
                    raise BudgetDenied("run_trace_changed")
                seen.add(target)
                row = await session.get(TaskRunRecord, target)
                if row is None or row.user_id != user_id:
                    raise BudgetDenied("budget_run_not_found")
                rows.append(row)
                target = row.parent_run_id
            if not rows[-1].budget or (
                rows[-1].budget.get("enabled") is not True
                and rows[-1].budget.get("enabled") is not False
            ):
                raise BudgetDenied("budget_snapshot_missing")
            if rows[-1].budget.get("enabled") is True:
                if require_disabled:
                    raise BudgetDenied("run_trace_changed")
                return None
            trace = DisabledRunTrace(
                id(database),
                user_id,
                tuple(trace_source(row, maintenance=maintenance) for row in reversed(rows)),
                expires_at,
            )
            await require_run_trace(
                session, trace, user_id=user_id, privacy_level=PrivacyLevel(rows[0].privacy_level)
            )
            return trace

    return await joined_read(read())


def extend_run_trace(trace: DisabledRunTrace, row: TaskRunRecord) -> DisabledRunTrace:
    if (
        len(trace.sources) >= 32
        or row.parent_run_id != trace.run_id
        or row.user_id != trace.user_id
    ):
        raise BudgetDenied("run_trace_changed")
    return replace(trace, sources=(*trace.sources, trace_source(row, maintenance=False)))
