"""Domain ORM mappings: workflows; registered once on shared Base.metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class WorkflowRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """FLOW-01 可复用流程：已注册动作的持久化模板。

    steps 是 ActionInvocation 形状的有序列表（action_id + 固定参数）；
    保存与运行时都经 ActionRegistry 重新编译校验，注册表变更会让
    过期模板在预览/运行时显式失败而不是带病执行。
    """

    __tablename__ = "workflow"
    __table_args__ = (
        Index("ix_workflow_user", "user_id"),
        UniqueConstraint("user_id", "name", name="uq_workflow_user_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WorkflowDraftRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """DIST（docs/09 §4）计划轨迹蒸馏出的流程草稿：仅人工审阅后可用。

    来源是全部步骤验证通过的行动计划；审批硬门槛是 A0 只读步骤的
    样例回放通过（结构一致），不存在自动晋级。dedupe_key 按步骤内容
    幂等，同一轨迹重复完成不重复生成草稿。
    """

    __tablename__ = "workflow_draft"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','dismissed')",
            name="ck_workflow_draft_status",
        ),
        CheckConstraint(
            "replay_status IN ('not_run','passed','failed','not_applicable')",
            name="ck_workflow_draft_replay",
        ),
        Index("ix_workflow_draft_user", "user_id"),
        UniqueConstraint("dedupe_key", name="uq_workflow_draft_dedupe"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    # 蒸馏来源计划；计划删除后草稿保留（步骤快照自足）
    plan_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    replay_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="not_run")
    replay_detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
