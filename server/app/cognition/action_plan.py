from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue
from sqlalchemy import select

from app.db import ActionPlanRecord, ActionStepRecord, AppUserRecord, Database
from app.ids import uuid7
from app.schemas.common import StrictModel, TokenName
from app.tools import ToolResult

from .action_registry import ActionRegistry, ConfirmationPolicy

logger = logging.getLogger(__name__)


class ActionPlanStatus(StrEnum):
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    READY = "ready"
    EXECUTING = "executing"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ActionStepStatus(StrEnum):
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    READY = "ready"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    UNKNOWN_OUTCOME = "unknown_outcome"
    EXPIRED = "expired"


class ActionVerificationStatus(StrEnum):
    PENDING = "pending"
    NOT_REQUIRED = "not_required"
    VERIFIED = "verified"
    INCONCLUSIVE = "inconclusive"


class ActionRunResult(StrictModel):
    execution: ToolResult
    verification_status: ActionVerificationStatus = ActionVerificationStatus.NOT_REQUIRED
    verification_result: dict[str, JsonValue] | None = None
    verification_reason_code: str | None = None


class ActionInvocation(StrictModel):
    action_id: TokenName
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ActionStepView(StrictModel):
    id: UUID
    position: int
    action_id: str
    risk: str
    confirmation_policy: str
    status: str
    arguments: dict[str, JsonValue]
    tool_name: str
    tool_arguments: dict[str, JsonValue]
    idempotency_key: str
    timeout_seconds: int
    verification_policy: str
    verifier_id: str | None = None
    compensation_action_id: str | None = None
    compensates_step_id: UUID | None = None
    result: dict[str, JsonValue] | None = None
    verification_status: str
    verification_result: dict[str, JsonValue] | None = None
    verified_at: datetime | None = None
    reason_code: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ActionPlanView(StrictModel):
    id: UUID
    user_id: UUID
    title: str | None = None
    plan_kind: str
    source_plan_id: UUID | None = None
    undo_plan_id: UUID | None = None
    status: str
    idempotency_key: str
    expires_at: datetime
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None
    completed_at: datetime | None = None
    cancel_reason: str | None = None
    # PC-02：执行中收到停止请求时保持 executing，但置位此字段供 UI 展示。
    cancel_requested: bool = False
    reason_code: str | None = None
    created_at: datetime
    updated_at: datetime
    steps: list[ActionStepView]


class PlanExecutionEvent(StrictModel):
    """PC-02 执行进度事件：只含目标/步骤/进度/证据状态，绝不含步骤参数。"""

    plan_id: UUID
    user_id: UUID
    phase: Literal["execution.started", "step.started", "step.finished", "execution.finished"]
    plan_status: str
    step_position: int | None = None
    step_action_id: str | None = None
    step_status: str | None = None
    step_verification: str | None = None
    completed: int = 0
    total: int = 0
    reason_code: str | None = None


ExecutionListener = Callable[[PlanExecutionEvent], Awaitable[None]]


class ActionPlanService:
    """Persistent control plane for plan creation, confirmation and cancellation."""

    def __init__(
        self,
        database: Database,
        registry: ActionRegistry,
        *,
        preauthorized_action_ids: frozenset[str] = frozenset(),
        runner: Callable[[ActionStepView, UUID], Awaitable[ActionRunResult | ToolResult]]
        | None = None,
    ) -> None:
        self._database = database
        self._registry = registry
        self._preauthorized = preauthorized_action_ids
        self._runner = runner
        self._execution_listener: ExecutionListener | None = None

    def set_runner(
        self,
        runner: Callable[[ActionStepView, UUID], Awaitable[ActionRunResult | ToolResult]],
    ) -> None:
        self._runner = runner

    def set_execution_listener(self, listener: ExecutionListener) -> None:
        """PC-02：订阅执行进度事件；监听器异常绝不影响执行本身。"""
        self._execution_listener = listener

    async def create_plan(
        self,
        *,
        user_id: UUID,
        invocations: list[ActionInvocation],
        title: str | None = None,
        ttl_seconds: Annotated[int, Field(ge=30, le=3_600)] = 300,
        idempotency_key: str | None = None,
        plan_kind: str = "standard",
        source_plan_id: UUID | None = None,
        compensates_step_ids: list[UUID] | None = None,
    ) -> ActionPlanView:
        if not 1 <= len(invocations) <= 10:
            raise ValueError("action plans require between 1 and 10 steps")
        if plan_kind not in {"standard", "compensation"}:
            raise ValueError("unsupported action plan kind")
        if compensates_step_ids is not None and len(compensates_step_ids) != len(invocations):
            raise ValueError("compensation step links must match action steps")
        compiled = [
            self._registry.compile(item.action_id, dict(item.arguments)) for item in invocations
        ]
        request_hash = _request_hash(title, invocations, ttl_seconds)
        resolved_key = idempotency_key or f"action-plan-{uuid7()}"
        if not 8 <= len(resolved_key) <= 160:
            raise ValueError("action plan idempotency key must be 8..160 characters")

        async with self._database.sessions() as session:
            existing = await session.scalar(
                select(ActionPlanRecord).where(
                    ActionPlanRecord.user_id == user_id,
                    ActionPlanRecord.idempotency_key == resolved_key,
                )
            )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ValueError("idempotency key was already used for another action plan")
            return await self.get_plan(user_id=user_id, plan_id=existing.id)

        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)
        plan_id = uuid7()
        step_records: list[ActionStepRecord] = []
        awaiting_confirmation = False
        for position, item in enumerate(compiled, start=1):
            definition = item.definition
            requires_confirmation = definition.confirmation_policy == ConfirmationPolicy.ALWAYS or (
                definition.confirmation_policy == ConfirmationPolicy.PREAUTHORIZED
                and definition.action_id not in self._preauthorized
            )
            status = (
                ActionStepStatus.AWAITING_CONFIRMATION.value
                if requires_confirmation
                else ActionStepStatus.READY.value
            )
            awaiting_confirmation = awaiting_confirmation or requires_confirmation
            step_records.append(
                ActionStepRecord(
                    id=uuid7(),
                    plan_id=plan_id,
                    position=position,
                    action_id=definition.action_id,
                    risk=str(definition.risk),
                    confirmation_policy=str(definition.confirmation_policy),
                    status=status,
                    arguments=dict(item.arguments),
                    tool_name=definition.tool_name,
                    tool_arguments=dict(item.tool_arguments),
                    idempotency_key=f"{plan_id}:{position}:{definition.action_id}",
                    timeout_seconds=definition.timeout_seconds,
                    verification_policy=str(definition.verification_policy),
                    verifier_id=definition.verifier_id,
                    compensation_action_id=definition.compensation_action_id,
                    compensates_step_id=(
                        compensates_step_ids[position - 1]
                        if compensates_step_ids is not None
                        else None
                    ),
                    verification_status=(
                        ActionVerificationStatus.NOT_REQUIRED.value
                        if definition.verification_policy == "none"
                        else ActionVerificationStatus.PENDING.value
                    ),
                )
            )
        plan = ActionPlanRecord(
            id=plan_id,
            user_id=user_id,
            title=title.strip() if title else None,
            plan_kind=plan_kind,
            source_plan_id=source_plan_id,
            status=(
                ActionPlanStatus.AWAITING_CONFIRMATION.value
                if awaiting_confirmation
                else ActionPlanStatus.READY.value
            ),
            idempotency_key=resolved_key,
            request_hash=request_hash,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        async with self._database.sessions.begin() as session:
            user_exists = await session.scalar(
                select(AppUserRecord.id).where(AppUserRecord.id == user_id)
            )
            if user_exists is None:
                raise LookupError("active user not found")
            session.add(plan)
            session.add_all(step_records)
        return _plan_view(plan, step_records)

    async def get_plan(self, *, user_id: UUID, plan_id: UUID) -> ActionPlanView:
        async with self._database.sessions() as session:
            plan = await session.get(ActionPlanRecord, plan_id)
            if plan is None or plan.user_id != user_id:
                raise LookupError("action plan not found")
            steps = list(
                await session.scalars(
                    select(ActionStepRecord)
                    .where(ActionStepRecord.plan_id == plan_id)
                    .order_by(ActionStepRecord.position)
                )
            )
        return _plan_view(plan, steps)

    async def confirm_plan(self, *, user_id: UUID, plan_id: UUID) -> ActionPlanView:
        now = datetime.now(UTC)
        expired = False
        async with self._database.sessions.begin() as session:
            plan = await session.scalar(
                select(ActionPlanRecord).where(ActionPlanRecord.id == plan_id).with_for_update()
            )
            if plan is None or plan.user_id != user_id:
                raise LookupError("action plan not found")
            steps = list(
                await session.scalars(
                    select(ActionStepRecord)
                    .where(ActionStepRecord.plan_id == plan_id)
                    .order_by(ActionStepRecord.position)
                    .with_for_update()
                )
            )
            if _as_utc(plan.expires_at) <= now:
                plan.status = ActionPlanStatus.EXPIRED.value
                plan.updated_at = now
                for step in steps:
                    if step.status in {
                        ActionStepStatus.AWAITING_CONFIRMATION.value,
                        ActionStepStatus.READY.value,
                    }:
                        step.status = ActionStepStatus.EXPIRED.value
                        step.completed_at = now
                expired = True
            elif plan.status != ActionPlanStatus.AWAITING_CONFIRMATION.value:
                raise ValueError("only awaiting action plans can be confirmed")
            else:
                for step in steps:
                    if step.status == ActionStepStatus.AWAITING_CONFIRMATION.value:
                        step.status = ActionStepStatus.READY.value
                plan.status = ActionPlanStatus.READY.value
                plan.confirmed_at = now
                plan.updated_at = now
        if expired:
            raise ValueError("action plan expired")
        return _plan_view(plan, steps)

    async def cancel_plan(
        self,
        *,
        user_id: UUID,
        plan_id: UUID,
        reason: str = "user_cancelled",
    ) -> ActionPlanView:
        now = datetime.now(UTC)
        # PC-02：执行中取消在置位协作标记后提前提交返回（不能在事务内重读）。
        executing_cancel = False
        async with self._database.sessions.begin() as session:
            plan = await session.scalar(
                select(ActionPlanRecord).where(ActionPlanRecord.id == plan_id).with_for_update()
            )
            if plan is None or plan.user_id != user_id:
                raise LookupError("action plan not found")
            if plan.status == ActionPlanStatus.EXECUTING.value:
                # 执行中停止：只置位协作标记，由执行循环在下一个步骤边界收敛
                # （在途步骤按自身超时结束，迟到结果不会推进后续步骤）。
                plan.cancel_requested = True
                plan.cancel_reason = reason[:160]
                plan.updated_at = now
                executing_cancel = True
            elif plan.status not in {
                ActionPlanStatus.AWAITING_CONFIRMATION.value,
                ActionPlanStatus.READY.value,
            }:
                raise ValueError("action plan can no longer be cancelled before execution")
        if executing_cancel:
            return await self.get_plan(user_id=user_id, plan_id=plan_id)
        async with self._database.sessions.begin() as session:
            plan = await session.scalar(
                select(ActionPlanRecord).where(ActionPlanRecord.id == plan_id).with_for_update()
            )
            if plan is None or plan.user_id != user_id:
                raise LookupError("action plan not found")
            steps = list(
                await session.scalars(
                    select(ActionStepRecord)
                    .where(ActionStepRecord.plan_id == plan_id)
                    .order_by(ActionStepRecord.position)
                    .with_for_update()
                )
            )
            plan.status = ActionPlanStatus.CANCELLED.value
            plan.cancelled_at = now
            plan.cancel_reason = reason[:160]
            plan.updated_at = now
            for step in steps:
                if step.status in {
                    ActionStepStatus.AWAITING_CONFIRMATION.value,
                    ActionStepStatus.READY.value,
                }:
                    step.status = ActionStepStatus.CANCELLED.value
                    step.completed_at = now
        return _plan_view(plan, steps)

    async def execute_plan(self, *, user_id: UUID, plan_id: UUID) -> ActionPlanView:
        if self._runner is None:
            raise RuntimeError("action execution runner is unavailable")
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            plan = await session.scalar(
                select(ActionPlanRecord).where(ActionPlanRecord.id == plan_id).with_for_update()
            )
            if plan is None or plan.user_id != user_id:
                raise LookupError("action plan not found")
            if _as_utc(plan.expires_at) <= now:
                raise ValueError("action plan expired")
            if plan.status != ActionPlanStatus.READY.value:
                raise ValueError("only ready action plans can execute")
            plan.status = ActionPlanStatus.EXECUTING.value
            plan.updated_at = now

        completed_count = 0
        total_steps = await self._count_steps(user_id=user_id, plan_id=plan_id)
        current_step_id: UUID | None = None
        await self._emit(
            PlanExecutionEvent(
                plan_id=plan_id,
                user_id=user_id,
                phase="execution.started",
                plan_status=ActionPlanStatus.EXECUTING.value,
                total=total_steps,
            )
        )
        try:
            while True:
                step = await self._claim_next_step(user_id=user_id, plan_id=plan_id)
                if step is None:
                    break
                current_step_id = step.id
                await self._emit(
                    PlanExecutionEvent(
                        plan_id=plan_id,
                        user_id=user_id,
                        phase="step.started",
                        plan_status=ActionPlanStatus.EXECUTING.value,
                        step_position=step.position,
                        step_action_id=step.action_id,
                        step_status=step.status,
                        completed=completed_count,
                        total=total_steps,
                    )
                )
                try:
                    async with asyncio.timeout(step.timeout_seconds):
                        raw_result = await self._runner(step, user_id)
                except TimeoutError:
                    await self._finish_step(
                        user_id=user_id,
                        plan_id=plan_id,
                        step_id=step.id,
                        status=ActionStepStatus.UNKNOWN_OUTCOME,
                        reason_code="action_timeout_unknown_outcome",
                        result=None,
                    )
                    return await self._fail_remaining_steps(
                        user_id=user_id,
                        plan_id=plan_id,
                        completed_count=completed_count,
                        reason_code="action_timeout_unknown_outcome",
                    )
                run_result = (
                    raw_result
                    if isinstance(raw_result, ActionRunResult)
                    else ActionRunResult(execution=raw_result)
                )
                result = run_result.execution
                if not result.ok:
                    await self._finish_step(
                        user_id=user_id,
                        plan_id=plan_id,
                        step_id=step.id,
                        status=ActionStepStatus.FAILED,
                        reason_code=result.reason_code or "action_tool_failed",
                        result=result.model_dump(mode="json"),
                        verification_status=run_result.verification_status,
                        verification_result=run_result.verification_result,
                    )
                    step_failed_status = ActionStepStatus.FAILED.value
                    await self._emit(
                        PlanExecutionEvent(
                            plan_id=plan_id,
                            user_id=user_id,
                            phase="step.finished",
                            plan_status=step_failed_status,
                            step_position=step.position,
                            step_action_id=step.action_id,
                            step_status=step_failed_status,
                            completed=completed_count,
                            total=total_steps,
                            reason_code=result.reason_code or "action_tool_failed",
                        )
                    )
                    return await self._fail_remaining_steps(
                        user_id=user_id,
                        plan_id=plan_id,
                        completed_count=completed_count,
                        reason_code=result.reason_code or "action_tool_failed",
                    )
                if run_result.verification_status == ActionVerificationStatus.INCONCLUSIVE:
                    reason_code = (
                        run_result.verification_reason_code or "action_verification_inconclusive"
                    )
                    await self._finish_step(
                        user_id=user_id,
                        plan_id=plan_id,
                        step_id=step.id,
                        status=ActionStepStatus.UNKNOWN_OUTCOME,
                        reason_code=reason_code,
                        result=result.model_dump(mode="json"),
                        verification_status=run_result.verification_status,
                        verification_result=run_result.verification_result,
                    )
                    await self._emit(
                        PlanExecutionEvent(
                            plan_id=plan_id,
                            user_id=user_id,
                            phase="step.finished",
                            plan_status=ActionStepStatus.UNKNOWN_OUTCOME.value,
                            step_position=step.position,
                            step_action_id=step.action_id,
                            step_status=ActionStepStatus.UNKNOWN_OUTCOME.value,
                            step_verification=str(run_result.verification_status),
                            completed=completed_count,
                            total=total_steps,
                            reason_code=reason_code,
                        )
                    )
                    return await self._fail_remaining_steps(
                        user_id=user_id,
                        plan_id=plan_id,
                        completed_count=completed_count,
                        reason_code=reason_code,
                    )
                await self._finish_step(
                    user_id=user_id,
                    plan_id=plan_id,
                    step_id=step.id,
                    status=ActionStepStatus.COMPLETED,
                    reason_code=None,
                    result=result.model_dump(mode="json"),
                    verification_status=run_result.verification_status,
                    verification_result=run_result.verification_result,
                )
                completed_count += 1
                current_step_id = None
                await self._emit(
                    PlanExecutionEvent(
                        plan_id=plan_id,
                        user_id=user_id,
                        phase="step.finished",
                        plan_status=ActionPlanStatus.EXECUTING.value,
                        step_position=step.position,
                        step_action_id=step.action_id,
                        step_status=ActionStepStatus.COMPLETED.value,
                        step_verification=str(run_result.verification_status),
                        completed=completed_count,
                        total=total_steps,
                    )
                )
                if await self._cancel_requested(user_id=user_id, plan_id=plan_id):
                    # PC-02：停止请求已落库——本步结果如实保留，但不再启动后续步骤。
                    break
        except asyncio.CancelledError:
            if current_step_id is not None:
                await self._finish_step(
                    user_id=user_id,
                    plan_id=plan_id,
                    step_id=current_step_id,
                    status=ActionStepStatus.UNKNOWN_OUTCOME,
                    reason_code="execution_cancelled_unknown_outcome",
                    result=None,
                )
            await self._fail_remaining_steps(
                user_id=user_id,
                plan_id=plan_id,
                completed_count=completed_count,
                reason_code="execution_cancelled_unknown_outcome",
            )
            raise

        if await self._cancel_requested(user_id=user_id, plan_id=plan_id):
            view = await self._cancel_remaining_steps(
                user_id=user_id, plan_id=plan_id, completed_count=completed_count
            )
        else:
            async with self._database.sessions.begin() as session:
                plan = await session.scalar(
                    select(ActionPlanRecord)
                    .where(
                        ActionPlanRecord.id == plan_id,
                        ActionPlanRecord.user_id == user_id,
                    )
                    .with_for_update()
                )
                if plan is None:
                    raise LookupError("action plan not found")
                plan.status = ActionPlanStatus.COMPLETED.value
                plan.completed_at = datetime.now(UTC)
                plan.updated_at = plan.completed_at
            view = await self.get_plan(user_id=user_id, plan_id=plan_id)
        await self._emit(
            PlanExecutionEvent(
                plan_id=plan_id,
                user_id=user_id,
                phase="execution.finished",
                plan_status=view.status,
                completed=completed_count,
                total=total_steps,
                reason_code=view.reason_code,
            )
        )
        return view

    async def _claim_next_step(
        self,
        *,
        user_id: UUID,
        plan_id: UUID,
    ) -> ActionStepView | None:
        async with self._database.sessions.begin() as session:
            plan = await session.get(ActionPlanRecord, plan_id)
            if plan is None or plan.user_id != user_id:
                raise LookupError("action plan not found")
            if plan.status != ActionPlanStatus.EXECUTING.value:
                raise ValueError("action plan is not executing")
            step = await session.scalar(
                select(ActionStepRecord)
                .where(
                    ActionStepRecord.plan_id == plan_id,
                    ActionStepRecord.status == ActionStepStatus.READY.value,
                )
                .order_by(ActionStepRecord.position)
                .limit(1)
                .with_for_update()
            )
            if step is None:
                return None
            step.status = ActionStepStatus.EXECUTING.value
            step.started_at = datetime.now(UTC)
        return _step_view(step)

    async def _finish_step(
        self,
        *,
        user_id: UUID,
        plan_id: UUID,
        step_id: UUID,
        status: ActionStepStatus,
        reason_code: str | None,
        result: dict[str, JsonValue] | None,
        verification_status: ActionVerificationStatus | str | None = None,
        verification_result: dict[str, JsonValue] | None = None,
    ) -> None:
        async with self._database.sessions.begin() as session:
            plan = await session.get(ActionPlanRecord, plan_id)
            step = await session.get(ActionStepRecord, step_id)
            if plan is None or plan.user_id != user_id or step is None or step.plan_id != plan_id:
                raise LookupError("action step not found")
            if step.status != ActionStepStatus.EXECUTING.value:
                raise ValueError("only executing action steps can finish")
            step.status = status.value
            step.reason_code = reason_code
            step.result = result
            if verification_status is not None:
                step.verification_status = str(verification_status)
                step.verification_result = verification_result
                if verification_status == ActionVerificationStatus.VERIFIED:
                    step.verified_at = datetime.now(UTC)
            step.completed_at = datetime.now(UTC)

    async def create_undo_plan(
        self,
        *,
        user_id: UUID,
        plan_id: UUID,
        ttl_seconds: Annotated[int, Field(ge=30, le=3_600)] = 300,
    ) -> ActionPlanView:
        original = await self.get_plan(user_id=user_id, plan_id=plan_id)
        if original.plan_kind != "standard":
            raise ValueError("compensation plans cannot be undone recursively")
        if original.status not in {
            ActionPlanStatus.COMPLETED.value,
            ActionPlanStatus.PARTIALLY_COMPLETED.value,
        }:
            raise ValueError("only completed action plans can be undone")
        if original.undo_plan_id is not None:
            return await self.get_plan(user_id=user_id, plan_id=original.undo_plan_id)
        reversible_steps = [
            step
            for step in reversed(original.steps)
            if step.status == ActionStepStatus.COMPLETED.value
            and step.compensation_action_id is not None
        ]
        if not reversible_steps:
            raise ValueError("action plan has no completed reversible steps")
        undo = await self.create_plan(
            user_id=user_id,
            title=f"撤销：{original.title or str(original.id)}",
            invocations=[
                ActionInvocation(
                    action_id=step.compensation_action_id or "",
                    arguments=step.arguments,
                )
                for step in reversible_steps
            ],
            ttl_seconds=ttl_seconds,
            idempotency_key=f"undo:{original.id}",
            plan_kind="compensation",
            source_plan_id=original.id,
            compensates_step_ids=[step.id for step in reversible_steps],
        )
        async with self._database.sessions.begin() as session:
            record = await session.scalar(
                select(ActionPlanRecord)
                .where(
                    ActionPlanRecord.id == original.id,
                    ActionPlanRecord.user_id == user_id,
                )
                .with_for_update()
            )
            if record is None:
                raise LookupError("action plan not found")
            record.undo_plan_id = undo.id
            record.updated_at = datetime.now(UTC)
        return undo

    async def _fail_remaining_steps(
        self,
        *,
        user_id: UUID,
        plan_id: UUID,
        completed_count: int,
        reason_code: str,
    ) -> ActionPlanView:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            plan = await session.scalar(
                select(ActionPlanRecord).where(ActionPlanRecord.id == plan_id).with_for_update()
            )
            if plan is None or plan.user_id != user_id:
                raise LookupError("action plan not found")
            remaining = list(
                await session.scalars(
                    select(ActionStepRecord)
                    .where(
                        ActionStepRecord.plan_id == plan_id,
                        ActionStepRecord.status == ActionStepStatus.READY.value,
                    )
                    .with_for_update()
                )
            )
            cancelled_intent = plan.cancel_requested
            for step in remaining:
                if cancelled_intent:
                    # PC-02：用户已请求停止——剩余步骤记 cancelled 而不是 skipped。
                    step.status = ActionStepStatus.CANCELLED.value
                    step.reason_code = "user_cancelled"
                else:
                    step.status = ActionStepStatus.SKIPPED.value
                    step.reason_code = "previous_step_failed"
                step.completed_at = now
            if cancelled_intent:
                plan.status = ActionPlanStatus.CANCELLED.value
                plan.cancelled_at = now
            else:
                plan.status = (
                    ActionPlanStatus.PARTIALLY_COMPLETED.value
                    if completed_count
                    else ActionPlanStatus.FAILED.value
                )
            plan.completed_at = now
            plan.updated_at = now
            plan.reason_code = reason_code[:160]
        return await self.get_plan(user_id=user_id, plan_id=plan_id)

    async def _count_steps(self, *, user_id: UUID, plan_id: UUID) -> int:
        async with self._database.sessions() as session:
            plan = await session.get(ActionPlanRecord, plan_id)
            if plan is None or plan.user_id != user_id:
                raise LookupError("action plan not found")
            return len(
                list(
                    await session.scalars(
                        select(ActionStepRecord.id).where(ActionStepRecord.plan_id == plan_id)
                    )
                )
            )

    async def _cancel_requested(self, *, user_id: UUID, plan_id: UUID) -> bool:
        async with self._database.sessions() as session:
            plan = await session.get(ActionPlanRecord, plan_id)
            if plan is None or plan.user_id != user_id:
                raise LookupError("action plan not found")
            return bool(plan.cancel_requested)

    async def _cancel_remaining_steps(
        self,
        *,
        user_id: UUID,
        plan_id: UUID,
        completed_count: int,
    ) -> ActionPlanView:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            plan = await session.scalar(
                select(ActionPlanRecord).where(ActionPlanRecord.id == plan_id).with_for_update()
            )
            if plan is None or plan.user_id != user_id:
                raise LookupError("action plan not found")
            remaining = list(
                await session.scalars(
                    select(ActionStepRecord)
                    .where(
                        ActionStepRecord.plan_id == plan_id,
                        ActionStepRecord.status == ActionStepStatus.READY.value,
                    )
                    .with_for_update()
                )
            )
            for step in remaining:
                step.status = ActionStepStatus.CANCELLED.value
                step.reason_code = "user_cancelled"
                step.completed_at = now
            plan.status = ActionPlanStatus.CANCELLED.value
            plan.cancelled_at = now
            plan.completed_at = now
            plan.updated_at = now
            if not plan.cancel_reason:
                plan.cancel_reason = "user_cancelled"
        return await self.get_plan(user_id=user_id, plan_id=plan_id)

    async def _emit(self, event: PlanExecutionEvent) -> None:
        if self._execution_listener is None:
            return
        try:
            await self._execution_listener(event)
        except Exception:
            # 进度事件是增强路径：监听器故障不能中断计划执行。
            logger.exception("plan execution listener failed")


def _request_hash(
    title: str | None,
    invocations: list[ActionInvocation],
    ttl_seconds: int,
) -> str:
    payload = {
        "title": title.strip() if title else None,
        "steps": [item.model_dump(mode="json") for item in invocations],
        "ttl_seconds": ttl_seconds,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _plan_view(
    plan: ActionPlanRecord,
    steps: list[ActionStepRecord],
) -> ActionPlanView:
    return ActionPlanView(
        id=plan.id,
        user_id=plan.user_id,
        title=plan.title,
        plan_kind=plan.plan_kind,
        source_plan_id=plan.source_plan_id,
        undo_plan_id=plan.undo_plan_id,
        status=plan.status,
        idempotency_key=plan.idempotency_key,
        expires_at=plan.expires_at,
        confirmed_at=plan.confirmed_at,
        cancelled_at=plan.cancelled_at,
        completed_at=plan.completed_at,
        cancel_reason=plan.cancel_reason,
        cancel_requested=plan.cancel_requested,
        reason_code=plan.reason_code,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        steps=[_step_view(step) for step in steps],
    )


def _step_view(step: ActionStepRecord) -> ActionStepView:
    return ActionStepView(
        id=step.id,
        position=step.position,
        action_id=step.action_id,
        risk=step.risk,
        confirmation_policy=step.confirmation_policy,
        status=step.status,
        arguments=step.arguments,
        tool_name=step.tool_name,
        tool_arguments=step.tool_arguments,
        idempotency_key=step.idempotency_key,
        timeout_seconds=step.timeout_seconds,
        verification_policy=step.verification_policy,
        verifier_id=step.verifier_id,
        compensation_action_id=step.compensation_action_id,
        compensates_step_id=step.compensates_step_id,
        result=step.result,
        verification_status=step.verification_status,
        verification_result=step.verification_result,
        verified_at=step.verified_at,
        reason_code=step.reason_code,
        started_at=step.started_at,
        completed_at=step.completed_at,
    )
