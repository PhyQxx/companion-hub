"""Domain ORM mappings: actions; registered once on shared Base.metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ActionPlanRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "action_plan"
    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_confirmation','ready','executing','completed',"
            "'partially_completed','failed','cancelled','expired')",
            name="ck_action_plan_status",
        ),
        CheckConstraint(
            "plan_kind IN ('standard','compensation')",
            name="ck_action_plan_kind",
        ),
        UniqueConstraint("user_id", "idempotency_key", name="uq_action_plan_user_idempotency"),
        Index("ix_action_plan_user_created", "user_id", "created_at"),
        Index("ix_action_plan_user_status", "user_id", "status", "created_at"),
    )

    task_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_run.id", ondelete="SET NULL"),
        index=True,
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(240))
    plan_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, default="standard", server_default="standard"
    )
    # PC-02 执行中停止：协作式取消标记。取消请求落库后，执行循环不再启动
    # 新步骤；在途步骤按自身超时收敛，迟到结果不会推进后续步骤。
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    source_plan_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("action_plan.id", ondelete="SET NULL")
    )
    undo_plan_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("action_plan.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(160))
    reason_code: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ActionStepRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "action_step"
    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_confirmation','ready','executing','completed','failed',"
            "'cancelled','skipped','unknown_outcome','expired')",
            name="ck_action_step_status",
        ),
        CheckConstraint(
            "verification_status IN ('pending','not_required','verified','inconclusive')",
            name="ck_action_step_verification_status",
        ),
        UniqueConstraint("plan_id", "position", name="uq_action_step_plan_position"),
        UniqueConstraint("idempotency_key", name="uq_action_step_idempotency"),
        Index("ix_action_step_plan_status", "plan_id", "status", "position"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    plan_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("action_plan.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    action_id: Mapped[str] = mapped_column(String(160), nullable=False)
    risk: Mapped[str] = mapped_column(String(4), nullable=False)
    confirmation_policy: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_arguments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    verification_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    verifier_id: Mapped[str | None] = mapped_column(String(160))
    compensation_action_id: Mapped[str | None] = mapped_column(String(160))
    compensates_step_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("action_step.id", ondelete="SET NULL")
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    verification_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    verification_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_code: Mapped[str | None] = mapped_column(String(160))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PendingMutationRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """FIX-01/01B 待确认预览的持久化形态：邮件/日程/流程共享同一底座。

    跨重启/跨 worker 存活；认领以 ``status='pending'`` 条件更新做原子守卫，
    ``saving`` 中断遗留的行保持不确定终态语义由调用方写入。
    """

    __tablename__ = "pending_mutation"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','saving','completed','unknown_outcome','cancelled')",
            name="ck_pending_mutation_status",
        ),
        Index("ix_pending_mutation_user_status", "user_id", "status"),
        Index("ix_pending_mutation_expires", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    turn_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
