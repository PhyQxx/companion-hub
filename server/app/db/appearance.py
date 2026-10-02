"""Domain ORM mappings: appearance; registered once on shared Base.metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AvatarPackRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "avatar_pack"
    __table_args__ = (
        CheckConstraint(
            "engine IN ('static','live2d','vrm','abstract')",
            name="ck_avatar_pack_engine",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    archetype: Mapped[str] = mapped_column(String(64), nullable=False)
    engine: Mapped[str] = mapped_column(String(16), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    license: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    built_in: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AvatarInstanceRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "avatar_instance"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','preview','archived')",
            name="ck_avatar_instance_status",
        ),
        Index("ix_avatar_instance_owner", "owner"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner: Mapped[str] = mapped_column(String(160), nullable=False, server_default="local-user")
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    pack_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("avatar_pack.id", ondelete="RESTRICT"), nullable=False
    )
    customization: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, server_default="{}")
    voice_profile_id: Mapped[str | None] = mapped_column(String(160))
    theme_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PersonaAvatarBindingRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "persona_avatar_binding"
    __table_args__ = (
        Index(
            "uq_persona_avatar_default",
            "persona_id",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
    )

    persona_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("persona_version.id", ondelete="CASCADE"), primary_key=True
    )
    avatar_instance_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("avatar_instance.id", ondelete="CASCADE"), primary_key=True
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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
