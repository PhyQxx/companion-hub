"""Domain ORM mappings: calendar; registered once on shared Base.metadata."""

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
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class CalendarEventRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """CAL-01 日历事件：v1 以 Hub 本地库为唯一日历真源。

    会前提醒不是独立列，而是通过 task_item.source_ref=calendar:{id} 挂在
    TASK-01 调度底座上；事件改期/取消时由 CalendarService 联动重建/取消。
    """

    __tablename__ = "calendar_event"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','cancelled')",
            name="ck_calendar_event_status",
        ),
        Index("ix_calendar_event_user_start", "user_id", "status", "starts_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    calendar_id: Mapped[str] = mapped_column(String(64), nullable=False, default="primary")
    title: Mapped[str] = mapped_column(String(320), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    all_day: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    location: Mapped[str | None] = mapped_column(String(240))
    participants: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="api")
    # CAL-01 外部镜像（caldav 为远端真源时的本地投影）；本地事件为 NULL
    source_ref: Mapped[str | None] = mapped_column(String(160))
    external_etag: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CalendarOAuthTokenRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """CAL-01 外部日历 OAuth 刷新令牌（Google 等）：按用户+提供方唯一。

    刷新令牌只落库不进配置文档/日志；撤销 = 删除本行或 Google 端撤销授权。
    """

    __tablename__ = "calendar_oauth_token"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_calendar_oauth_user_provider"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    account_email: Mapped[str | None] = mapped_column(String(254))
    obtained_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ContactRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """CONTACT-01 联系人与关系上下文。

    只保存用户显式提供的字段；别名/时区/重要日期/偏好一律不随后台
    推断产生。名称与别名在用户内做应用层唯一性裁决（JSON 列无法建
    唯一索引），冲突直接拒绝而不是静默合并两个联系人。
    """

    __tablename__ = "contact"
    __table_args__ = (Index("ix_contact_user", "user_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    relationship: Mapped[str | None] = mapped_column(String(120))
    timezone: Mapped[str | None] = mapped_column(String(64))
    important_dates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    preferences: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
