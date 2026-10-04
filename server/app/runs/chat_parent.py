"""An attached chat cannot replace its owned root with a fresh quota."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, ConversationRecord, TaskRunRecord
from app.db.claims import assert_current_claim
from app.harness.budget import BudgetDenied
from app.harness.time import utc
from app.harness.voice_sources import VoiceRunFence, VoiceSourceClaim
from app.schemas import PrivacyLevel

from .parent_budget import ParentBudgetScope
from .voice_sources import require_voice_run_fences


def validate_parent(
    row: TaskRunRecord | None,
    *,
    user_id: UUID,
    conversation_id: UUID,
    privacy_level: PrivacyLevel,
    allow_succeeded: bool = False,
    quota_scope: dict[str, object] | None = None,
) -> TaskRunRecord:
    if row is None:
        raise BudgetDenied("budget_run_inactive")
    if row.user_id != user_id or row.conversation_id != conversation_id:
        raise BudgetDenied("chat_parent_scope_invalid")
    if quota_scope is not None:
        scope = ParentBudgetScope.read(quota_scope)
        if (
            scope.user_id != user_id
            or scope.conversation_id != conversation_id
            or row.parent_run_id != scope.run_id
            or row.contract.get("entry") != "voice.utterance"
            or row.contract.get("criterion") != "voice_reply_sent"
            or row.contract.get("quota_scope") != quota_scope
            or utc(scope.delivery_deadline) <= datetime.now(UTC)
        ):
            raise BudgetDenied("chat_parent_scope_invalid")
    elif row.parent_run_id is not None:
        raise BudgetDenied("chat_parent_must_be_root")
    if str(row.privacy_level) > str(privacy_level):
        raise BudgetDenied("operation_privacy_downgrade")
    statuses = {"accepted", "running", "succeeded"} if allow_succeeded else {"accepted", "running"}
    if row.status not in statuses or row.contract.get("work_cancel_requested"):
        raise BudgetDenied("budget_run_inactive")
    if row.contract.get("budget_usage_overflow"):
        raise BudgetDenied("budget_usage_overflow")
    if not row.budget or "enabled" not in row.budget:
        raise BudgetDenied("budget_snapshot_missing")
    config = RunBudgetConfig.model_validate(row.budget)
    if not (allow_succeeded and row.status == "succeeded") and (
        (config.enabled and row.deadline is None)
        or (row.deadline is not None and utc(row.deadline) <= datetime.now(UTC))
    ):
        raise BudgetDenied("run_deadline_exceeded")
    return row


async def require_chat_parent(
    session: AsyncSession,
    parent_id: UUID,
    *,
    user_id: UUID,
    conversation_id: UUID,
    privacy_level: PrivacyLevel,
    lock: bool = False,
    allow_succeeded: bool = False,
    child_id: UUID | None = None,
    quota_scope: dict[str, object] | None = None,
) -> TaskRunRecord:
    await assert_current_claim(session)
    if lock:
        # Write first: preserve claim -> owner -> root -> conversation order
        # without upgrading an earlier SQLite read transaction.
        owner = await session.scalar(
            update(AppUserRecord)
            .where(AppUserRecord.id == user_id, AppUserRecord.status == "active")
            .values(id=AppUserRecord.id)
            .returning(AppUserRecord.id)
        )
        if owner is None:
            raise BudgetDenied("budget_owner_invalid")
        if quota_scope is not None:
            scope = ParentBudgetScope.read(quota_scope)
            if scope.user_id != user_id:
                raise BudgetDenied("chat_parent_scope_invalid")
            quota_id = await session.scalar(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == scope.run_id, TaskRunRecord.user_id == user_id)
                .values(updated_at=TaskRunRecord.updated_at)
                .returning(TaskRunRecord.id)
            )
            if quota_id is None:
                raise BudgetDenied("budget_run_inactive")
        row = await session.scalar(
            update(TaskRunRecord)
            .where(TaskRunRecord.id == parent_id, TaskRunRecord.user_id == user_id)
            .values(updated_at=TaskRunRecord.updated_at)
            .returning(TaskRunRecord)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
    else:
        query = (
            select(TaskRunRecord)
            .join(AppUserRecord, AppUserRecord.id == TaskRunRecord.user_id)
            .join(ConversationRecord, ConversationRecord.id == TaskRunRecord.conversation_id)
            .where(
                TaskRunRecord.id == parent_id,
                AppUserRecord.status == "active",
                ConversationRecord.user_id == user_id,
                ConversationRecord.status == "active",
            )
        )
        if child_id is not None:
            child = aliased(TaskRunRecord)
            query = query.join(
                child,
                (child.parent_run_id == TaskRunRecord.id)
                & (child.id == child_id)
                & (child.user_id == user_id)
                & (child.conversation_id == conversation_id)
                & (child.contract["budget_parent_id"].as_string() == str(parent_id)),
            )
        row = await session.scalar(query)
        if row is None and child_id is not None:
            raise BudgetDenied("chat_parent_changed")
    parent = validate_parent(
        row,
        user_id=user_id,
        conversation_id=conversation_id,
        privacy_level=privacy_level,
        allow_succeeded=allow_succeeded,
        quota_scope=quota_scope,
    )
    if child_id is not None and lock:
        child = await session.scalar(
            update(TaskRunRecord)
            .where(TaskRunRecord.id == child_id, TaskRunRecord.user_id == user_id)
            .values(updated_at=TaskRunRecord.updated_at)
            .returning(TaskRunRecord)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        if (
            child is None
            or child.user_id != user_id
            or child.conversation_id != conversation_id
            or child.parent_run_id != parent_id
            or child.contract.get("budget_parent_id") != str(parent_id)
            or child.contract.get("quota_scope") != quota_scope
        ):
            raise BudgetDenied("chat_parent_changed")
        # A child lock wait can expire the root that we locked first.
        validate_parent(
            parent,
            user_id=user_id,
            conversation_id=conversation_id,
            privacy_level=privacy_level,
            allow_succeeded=allow_succeeded,
            quota_scope=quota_scope,
        )
    if quota_scope is not None:
        scope = ParentBudgetScope.read(quota_scope)
        quota = await session.get(TaskRunRecord, scope.run_id)
        if quota is None or quota.parent_run_id is not None:
            raise BudgetDenied("chat_parent_scope_invalid")
        actor = scope.source_actor
        if actor not in {"browser", "satellite"} or scope.source_id is None:
            raise BudgetDenied("voice_actor_invalid")
        source = VoiceSourceClaim(
            user_id,
            conversation_id,
            "browser" if actor == "browser" else "satellite",
            scope.source_id,
            privacy_level,
        )
        fences: tuple[VoiceRunFence, ...] = (
            VoiceRunFence(
                parent.id,
                bool(parent.budget and parent.budget.get("enabled")),
                check_lineage=True,
                parent_run_id=scope.run_id,
                conversation_id=conversation_id,
                quota_scope=quota_scope,
                allow_succeeded=allow_succeeded,
                check_source=True,
                source_actor=actor,
                source_id=scope.source_id,
            ),
            VoiceRunFence(
                scope.run_id,
                True,
                check_lineage=True,
                conversation_id=scope.quota_conversation_id,
                allow_succeeded=allow_succeeded,
            ),
        )
        if child_id is not None:
            child_row = await session.get(TaskRunRecord, child_id)
            if child_row is None:
                raise BudgetDenied("chat_parent_changed")
            fences += (
                VoiceRunFence(
                    child_id,
                    bool(child_row.budget and child_row.budget.get("enabled")),
                    check_lineage=True,
                    parent_run_id=parent_id,
                    conversation_id=conversation_id,
                    quota_scope=quota_scope,
                    allow_succeeded=allow_succeeded,
                ),
            )
        await require_voice_run_fences(session, source, fences)
        if utc(scope.delivery_deadline) <= datetime.now(UTC):
            raise BudgetDenied("run_deadline_exceeded")
    return parent
