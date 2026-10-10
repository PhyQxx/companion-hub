"""Domain ORM mappings: identity; registered once on shared Base.metadata."""

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
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AppUserRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint("role IN ('owner','member')", name="ck_app_user_role"),
        UniqueConstraint("sso_sub", name="uq_app_user_sso_sub"),
        UniqueConstraint("username", name="uq_app_user_username"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    locale: Mapped[str] = mapped_column(String(32), nullable=False, server_default="zh-CN")
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="Asia/Shanghai"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    # 本地账号名（用户名+密码登录用；SSO-only 用户为空），保存前经
    # normalize_username 统一小写。见 app.auth.service。
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # pnkx OIDC sub（即 pnkx userId）；本地密码通道初始化的存量用户在业主
    # 首次 SSO 登录时回填绑定，此后一个 pnkx 账号固定映射一个本地用户。
    sso_sub: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="member")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ObservationOwnerBindingRecord(Base):
    __module__ = "app.db.models"

    __tablename__ = "observation_owner_binding"
    __table_args__ = (CheckConstraint("slot = 1", name="ck_observation_owner_binding_slot"),)

    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Keep the minimal ownership evidence after account deletion. A foreign
    # key with cascade would silently enable reassignment of private sources.
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuthCredentialRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "auth_credential"
    __table_args__ = (
        CheckConstraint("kind IN ('password','passkey')", name="ck_auth_credential_kind"),
        CheckConstraint(
            "setup_slot IS NULL OR setup_slot = 1", name="ck_auth_credential_setup_slot"
        ),
        UniqueConstraint("setup_slot", name="uq_auth_credential_setup_slot"),
        Index("ix_auth_credential_user", "user_id", "revoked_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    credential_id: Mapped[bytes | None]
    public_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    secret_hash: Mapped[str | None] = mapped_column(Text)
    setup_slot: Mapped[int | None] = mapped_column(Integer)
    params_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSessionRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "auth_session"
    __table_args__ = (
        UniqueConstraint("access_hash", name="uq_auth_session_access_hash"),
        Index("ix_auth_session_user_active", "user_id", "expires_at", "revoked_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    device_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    access_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
