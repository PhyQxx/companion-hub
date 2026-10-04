"""Content-free source references retained when a quota crosses task boundaries."""

import hashlib
import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import TaskRunRecord
from app.harness.budget import BudgetDenied
from app.schemas import PrivacyLevel


def scope_fingerprint(value: object) -> str:
    # This contains only quota/source identifiers, limits and clocks, no content.
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class BudgetOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_run_id: UUID
    parent_run_id: UUID
    user_id: UUID
    conversation_id: UUID
    privacy_level: PrivacyLevel
    scope_fingerprint: str

    @classmethod
    def capture(cls, row: TaskRunRecord) -> "BudgetOrigin":
        if row.parent_run_id is None or row.conversation_id is None:
            raise BudgetDenied("chat_parent_scope_invalid")
        return cls(
            source_run_id=row.id,
            parent_run_id=row.parent_run_id,
            user_id=row.user_id,
            conversation_id=row.conversation_id,
            privacy_level=PrivacyLevel(row.privacy_level),
            scope_fingerprint=scope_fingerprint(row.contract.get("quota_scope")),
        )


async def require_budget_origins(
    session: AsyncSession,
    origins: tuple[BudgetOrigin, ...],
    *,
    run_id: UUID,
    user_id: UUID,
    privacy_level: PrivacyLevel | None = None,
    lock: bool = False,
) -> None:
    if not origins:
        return
    from .chat_parent import require_chat_parent
    from .parent_budget import ParentBudgetScope

    for origin in origins:
        if origin.user_id != user_id:
            raise BudgetDenied("budget_owner_invalid")
        source = await session.get(TaskRunRecord, origin.source_run_id, populate_existing=True)
        if (
            source is None
            or source.user_id != user_id
            or source.parent_run_id != origin.parent_run_id
            or source.conversation_id != origin.conversation_id
            or source.privacy_level != str(origin.privacy_level)
            or source.contract.get("budget_parent_id") != str(origin.parent_run_id)
            or scope_fingerprint(source.contract.get("quota_scope")) != origin.scope_fingerprint
        ):
            raise BudgetDenied("chat_parent_changed")
        if source.status not in {"accepted", "running", "succeeded"} or source.contract.get(
            "work_cancel_requested"
        ):
            raise BudgetDenied("budget_run_inactive")
        if source.contract.get("budget_usage_overflow"):
            raise BudgetDenied("budget_usage_overflow")
        if privacy_level is not None and str(origin.privacy_level) > str(privacy_level):
            raise BudgetDenied("operation_privacy_downgrade")
        raw_scope = source.contract.get("quota_scope")
        scope: ParentBudgetScope | None = (
            ParentBudgetScope.read(raw_scope) if raw_scope is not None else None
        )
        # Prove the wallet identity before acquiring any ancestor's locks.
        if (scope.run_id if scope else origin.parent_run_id) != run_id:
            raise BudgetDenied("budget_parent_scope_invalid")
        await require_chat_parent(
            session,
            origin.parent_run_id,
            user_id=user_id,
            conversation_id=origin.conversation_id,
            privacy_level=origin.privacy_level,
            child_id=origin.source_run_id,
            quota_scope=raw_scope,
            allow_succeeded=True,
            lock=lock,
        )
