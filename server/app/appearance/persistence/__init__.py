"""appearance 领域 SQLAlchemy 映射；注册在共享 Base.metadata（迁移自 app.db.appearance）。"""

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
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UiThemeRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "ui_theme"
    __table_args__ = (
        CheckConstraint(
            "status IN ('published','archived')",
            name="ck_ui_theme_status",
        ),
        UniqueConstraint("key", name="uq_ui_theme_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="published")
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    built_in: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UiPreferenceRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "ui_preference"
    __table_args__ = (
        CheckConstraint(
            "appearance_mode IN ('light','dark','system','scheduled')",
            name="ck_ui_preference_mode",
        ),
    )

    owner: Mapped[str] = mapped_column(String(160), primary_key=True)
    theme_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ui_theme.id", ondelete="RESTRICT"), nullable=False
    )
    appearance_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="light")
    # 定时主题（appearance_mode='scheduled'）的切换边界，HH:MM；其余模式为 NULL
    schedule_light_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    schedule_dark_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
