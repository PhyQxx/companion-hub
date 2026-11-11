"""contacts 领域 SQLAlchemy 映射；注册在共享 Base.metadata（迁移自 app.db.calendar）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


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
