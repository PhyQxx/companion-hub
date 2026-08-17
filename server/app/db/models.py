from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


class EventRecord(Base):
    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint("privacy_level IN ('L0','L1','L2')", name="ck_event_privacy_level"),
        Index("ix_event_conversation_created", "conversation_id", "created_at"),
        Index("ix_event_correlation", "correlation_id"),
    )

    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    proto_version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    causation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    turn_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    type: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    privacy_level: Mapped[str] = mapped_column(String(2), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutboxRecord(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','sending','dispatched','dead')",
            name="ck_outbox_status",
        ),
        UniqueConstraint("event_id", "topic", name="uq_outbox_event_topic"),
        Index("ix_outbox_dispatch", "status", "available_at", "lease_expires_at", "id"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("event.event_id", ondelete="RESTRICT"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class ConsumerInboxRecord(Base):
    __tablename__ = "consumer_inbox"

    consumer_name: Mapped[str] = mapped_column(String(160), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("event.event_id", ondelete="RESTRICT"), primary_key=True
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DeadLetterRecord(Base):
    __tablename__ = "dead_letter"
    __table_args__ = (
        UniqueConstraint("outbox_id", name="uq_dead_letter_outbox"),
        Index("ix_dead_letter_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    outbox_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("outbox.id", ondelete="RESTRICT"), nullable=False
    )
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str] = mapped_column(String(160), nullable=False)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConfigVersionRecord(Base):
    __tablename__ = "config_version"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','published','superseded')",
            name="ck_config_version_status",
        ),
        Index("ix_config_version_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollback_from_version: Mapped[int | None] = mapped_column(BigInteger)


class ConfigPointerRecord(Base):
    __tablename__ = "config_pointer"
    __table_args__ = (CheckConstraint("id = 1", name="ck_config_pointer_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    current_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("config_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
