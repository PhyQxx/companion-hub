"""Owned, versioned source for a single completion report; no action replay."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context.repository import ContextSourceInvalidated, validate_references
from app.db import (
    ActionPlanRecord,
    ActionStepRecord,
    ConversationRecord,
    DeletionLedgerRecord,
    MessageRecord,
    TaskRunRecord,
)
from app.harness.budget import BudgetDenied
from app.harness.context import ContextReference
from app.harness.time import utc
from app.runs.delivery_contracts import DeliverySourceSnapshot

from .action_plan import ActionPlanView, _plan_view

MAX_REPORT_STEPS = 1000


def completion_text(view: ActionPlanView, *, include_details: bool = True) -> str:
    if not include_details:
        return "计划执行记录已更新，请在计划详情查看结果。"
    title = view.title or "未命名计划"
    actions = "、".join(step.action_id for step in view.steps[:5])
    verified = bool(view.steps) and all(
        step.verification_status == "verified"
        and step.verification_policy == "read_after_write"
        and step.verifier_id == "home.entity_state"
        for step in view.steps
    )
    evidence = "实际状态已核对" if verified else "结果尚未全部核对"
    return f"计划「{title}」执行返回成功（{len(view.steps)} 步）：{actions}。{evidence}。"


@dataclass(frozen=True)
class PlanCompletionSource:
    source_id: UUID
    user_id: UUID
    version: datetime
    parent_id: UUID | None
    fingerprint: str
    message_id: UUID | None = None

    def request_key(self, entry: str, run_id: UUID) -> str:
        return f"{entry}:{self.source_id}"

    async def lock(self, session: AsyncSession, *, pending: bool = False) -> bool:
        return (
            await session.scalar(
                update(ActionPlanRecord)
                .where(
                    ActionPlanRecord.id == self.source_id,
                    ActionPlanRecord.user_id == self.user_id,
                    ActionPlanRecord.status == "completed",
                    ActionPlanRecord.cancel_requested.is_(False),
                )
                .values(id=ActionPlanRecord.id)
                .returning(ActionPlanRecord.id)
            )
            is not None
        )

    async def inspect(
        self,
        session: AsyncSession,
        fingerprint: str,
        run_id: UUID,
    ) -> DeliverySourceSnapshot:
        if fingerprint != self.fingerprint:
            raise BudgetDenied("delivery_source_changed")
        plan = await session.get(ActionPlanRecord, self.source_id)
        if (
            plan is None
            or plan.user_id != self.user_id
            or plan.status != "completed"
            or plan.cancel_requested
            or utc(plan.updated_at) != utc(self.version)
            or plan.task_run_id != self.parent_id
        ):
            raise BudgetDenied("delivery_source_changed")
        if self.parent_id is not None:
            parent = await session.get(TaskRunRecord, self.parent_id)
            if (
                parent is None
                or parent.user_id != self.user_id
                or parent.status not in {"accepted", "running", "succeeded"}
                or parent.contract.get("work_cancel_requested")
                or parent.privacy_level not in {"L0", "L1"}
            ):
                raise BudgetDenied("delivery_source_changed")
            if (
                parent.status != "succeeded"
                and parent.deadline is not None
                and utc(parent.deadline) <= datetime.now(UTC)
            ):
                raise BudgetDenied("delivery_source_changed")
            if parent.conversation_id is not None:
                conversation = await session.get(ConversationRecord, parent.conversation_id)
                if conversation is None or conversation.user_id != self.user_id:
                    raise BudgetDenied("delivery_source_changed")
                deleted = await session.scalar(
                    select(DeletionLedgerRecord.id)
                    .where(
                        DeletionLedgerRecord.entity_kind == "message",
                        func.lower(func.replace(DeletionLedgerRecord.entity_id, "-", ""))
                        == parent.conversation_id.hex,
                    )
                    .limit(1)
                )
                if deleted is not None:
                    raise BudgetDenied("delivery_source_changed")
            input_id = parent.contract.get("input_message_id")
            try:
                current_message_id = UUID(input_id) if input_id is not None else None
            except (ValueError, TypeError, AttributeError) as error:
                raise BudgetDenied("delivery_source_changed") from error
            if current_message_id != self.message_id:
                raise BudgetDenied("delivery_source_changed")
            if current_message_id is not None:
                message = await session.get(MessageRecord, current_message_id)
                if message is None or (
                    parent.conversation_id is not None
                    and message.conversation_id != parent.conversation_id
                ):
                    raise BudgetDenied("delivery_source_changed")
                try:
                    await validate_references(
                        session,
                        (
                            ContextReference(
                                "message",
                                str(current_message_id),
                                str(self.user_id),
                                parent.privacy_level,
                                version=str(message.seq),
                            ),
                        ),
                        owner_id=self.user_id,
                        privacy_level="L1",
                    )
                except ContextSourceInvalidated as error:
                    raise BudgetDenied("delivery_source_changed") from error
        steps = list(
            await session.scalars(
                select(ActionStepRecord)
                .where(ActionStepRecord.plan_id == self.source_id)
                .order_by(ActionStepRecord.position)
                .limit(MAX_REPORT_STEPS + 1)
            )
        )
        if (
            not steps
            or len(steps) > MAX_REPORT_STEPS
            or any(step.status != "completed" for step in steps)
        ):
            raise BudgetDenied("delivery_source_changed")
        text = completion_text(_plan_view(plan, steps), include_details=self.parent_id is not None)
        if hashlib.sha256(text.encode()).hexdigest() != fingerprint:
            raise BudgetDenied("delivery_source_changed")
        return DeliverySourceSnapshot("L1", source_parent_id=self.parent_id)

    async def finish(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        entry: str,
        channels: list[str],
        reason: str | None,
        state: str,
        now: datetime,
    ) -> bool:
        try:
            await self.inspect(session, self.fingerprint, run_id)
        except BudgetDenied:
            return True
        return False
