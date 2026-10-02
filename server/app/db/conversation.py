"""Domain ORM mappings: conversation; registered once on shared Base.metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
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

from .base import BIGINT_PK, Base


class ConversationRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "conversation"
    __table_args__ = (
        CheckConstraint("status IN ('active','archived')", name="ck_conversation_status"),
        Index("ix_conversation_user_active", "user_id", "last_active_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    # CTX 滚动会话摘要（docs/09 §6）：要点文本 + 覆盖到的消息 seq 水位
    summary_text: Mapped[str | None] = mapped_column(Text)
    summary_until_seq: Mapped[int | None] = mapped_column(BigInteger)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    last_turn_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MessageRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "message"
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant','system','tool')", name="ck_message_role"),
        CheckConstraint("privacy_level IN ('L0','L1','L2')", name="ck_message_privacy"),
        UniqueConstraint("conversation_id", "seq", name="uq_message_conversation_seq"),
        Index("ix_message_conversation_seq", "conversation_id", "seq"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversation.id", ondelete="RESTRICT"), nullable=False
    )
    turn_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    privacy_level: Mapped[str] = mapped_column(String(2), nullable=False)
    generation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    decision_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InteractionTurnRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "interaction_turn"
    __table_args__ = (
        CheckConstraint(
            "state IN ('accepted','listening','thinking','streaming','speaking',"
            "'interrupted','cancelled','failed','completed')",
            name="ck_interaction_turn_state",
        ),
        UniqueConstraint("conversation_id", "turn_seq", name="uq_turn_conversation_seq"),
        UniqueConstraint("generation_id", name="uq_turn_generation"),
        Index("ix_turn_conversation_state", "conversation_id", "state", "created_at"),
    )

    task_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_run.id", ondelete="SET NULL"),
        index=True,
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversation.id", ondelete="RESTRICT"), nullable=False
    )
    turn_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    input_message_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("message.id", ondelete="RESTRICT"), nullable=False
    )
    cancel_reason: Mapped[str | None] = mapped_column(String(160))
    degradation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuntimeLeaseRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "runtime_lease"
    __table_args__ = (Index("ix_runtime_lease_expires", "expires_at"),)

    lease_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    holder_device_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    generation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserModeRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "user_mode"
    __table_args__ = (Index("ix_user_mode_user_active", "user_id", "superseded_at", "priority"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(400))
    confidence: Mapped[float | None] = mapped_column(Float)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="50")
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
