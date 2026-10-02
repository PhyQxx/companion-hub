"""Domain ORM mappings: tasks; registered once on shared Base.metadata."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TaskItemRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """TASK-01 提醒与计划任务：持久化用户任务/提醒及其触发与投递状态。

    exactly-once 语义由 claim 时的乐观守卫（status + next_fire_at/last_fired_at 等值）
    保证；一次性任务触发期短暂处于 firing，投递结束后转 done。
    """

    __tablename__ = "task_item"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','firing','done','cancelled')",
            name="ck_task_item_status",
        ),
        CheckConstraint(
            "kind IN ('reminder','task')",
            name="ck_task_item_kind",
        ),
        CheckConstraint(
            "trigger_type IN ('time','event')",
            name="ck_task_item_trigger_type",
        ),
        CheckConstraint(
            "privacy_level IN ('L0','L1')",
            name="ck_task_item_privacy_level",
        ),
        Index("ix_task_item_user_status_created", "user_id", "status", "created_at"),
        Index("ix_task_item_due", "status", "trigger_type", "next_fire_at"),
        Index("ix_task_item_event", "status", "trigger_type", "event_type"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="reminder", server_default="reminder"
    )
    title: Mapped[str] = mapped_column(String(320), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(64))
    # 归属外部实体的可选引用（如 calendar:{event_id}），用于联动取消/重建
    source_ref: Mapped[str | None] = mapped_column(String(120))
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fire_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_delivery: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # TODO-01 外部任务镜像元数据（pnkx 为单一真源时的本地投影）
    external_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[int | None] = mapped_column(Integer)
    group_label: Mapped[str | None] = mapped_column(String(64))
    # 本地已改、尚未推回 pnkx 的优先级指令；推送成功后清空
    pending_priority: Mapped[int | None] = mapped_column(Integer)
    privacy_level: Mapped[str] = mapped_column(String(2), nullable=False, default="L1")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DailyBriefRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """BRIEF-01 每日智能简报：事实采集确定性、结论可溯源、每天至多一条。

    (user_id, brief_date) 唯一约束保证每日 exactly-once；facts 持久化每条
    结论的来源引用（task:{id} / goal:{id} / amap:weather:{adcode}）。
    """

    __tablename__ = "daily_brief"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','delivered')",
            name="ck_daily_brief_status",
        ),
        UniqueConstraint("user_id", "brief_date", name="uq_daily_brief_user_date"),
        Index("ix_daily_brief_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    brief_date: Mapped[date] = mapped_column(Date, nullable=False)
    facts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channels: Mapped[list[str] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DailyReviewRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """REVIEW-01 晚间回顾：完成/未完成/新承诺/明日重点四区块，逐项可修正。

    (user_id, review_date) 唯一约束保证每晚 exactly-once；items 持久化每条
    的来源引用与用户修正动作（confirmed/removed/note）。回顾只读事实、
    记录修正，绝不直接改写 Persona 或记忆。
    """

    __tablename__ = "daily_review"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','delivered')",
            name="ck_daily_review_status",
        ),
        UniqueConstraint("user_id", "review_date", name="uq_daily_review_user_date"),
        Index("ix_daily_review_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    review_date: Mapped[date] = mapped_column(Date, nullable=False)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channels: Mapped[list[str] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
