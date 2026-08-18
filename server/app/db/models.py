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
    false,
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


class PersonaVersionRecord(Base):
    __tablename__ = "persona_version"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','published','superseded')",
            name="ck_persona_version_status",
        ),
        Index("ix_persona_version_created", "created_at"),
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


class PersonaPointerRecord(Base):
    __tablename__ = "persona_pointer"
    __table_args__ = (CheckConstraint("id = 1", name="ck_persona_pointer_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    current_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("persona_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AppUserRecord(Base):
    __tablename__ = "app_user"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    locale: Mapped[str] = mapped_column(String(32), nullable=False, server_default="zh-CN")
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="Asia/Shanghai"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuthCredentialRecord(Base):
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


class ConversationRecord(Base):
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
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    last_turn_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MessageRecord(Base):
    __tablename__ = "message"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user','assistant','system','tool')", name="ck_message_role"
        ),
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


class MemoryRecord(Base):
    __tablename__ = "memory"
    __table_args__ = (
        CheckConstraint(
            "type IN ('episodic','semantic','preference','commitment','emotional')",
            name="ck_memory_type",
        ),
        CheckConstraint("privacy_level IN ('L0','L1','L2')", name="ck_memory_privacy"),
        CheckConstraint(
            "status IN ('active','archived','superseded','conflict')",
            name="ck_memory_status",
        ),
        CheckConstraint("importance >= 0 AND importance <= 1", name="ck_memory_importance"),
        Index("ix_memory_user_type_status", "user_id", "type", "status", "importance"),
        Index("ix_memory_superseded_by", "superseded_by"),
        Index("ix_memory_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    privacy_level: Mapped[str] = mapped_column(String(2), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(JSON)
    embedding_model: Mapped[str | None] = mapped_column(String(160))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)
    embedding_version: Mapped[str | None] = mapped_column(String(64))
    importance: Mapped[float] = mapped_column(nullable=False, default=0.5, server_default="0.5")
    pin: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=false())
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    confidence: Mapped[float | None] = mapped_column()
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("memory.id", ondelete="RESTRICT")
    )
    supersede_reason: Mapped[str | None] = mapped_column(String(400))
    conflict_with: Mapped[int | None] = mapped_column(BigInteger)
    extractor_version: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(160), nullable=False, server_default="system")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class MemorySourceRecord(Base):
    __tablename__ = "memory_source"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('message','event','memory','manual')",
            name="ck_memory_source_kind",
        ),
        Index("ix_memory_source_lookup", "source_kind", "source_id"),
    )

    memory_id: Mapped[int] = mapped_column(
        BIGINT_PK,
        ForeignKey("memory.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    excerpt_hash: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InteractionTurnRecord(Base):
    __tablename__ = "interaction_turn"
    __table_args__ = (
        CheckConstraint(
            "state IN ('accepted','thinking','streaming','cancelled','failed','completed')",
            name="ck_interaction_turn_state",
        ),
        UniqueConstraint("conversation_id", "turn_seq", name="uq_turn_conversation_seq"),
        UniqueConstraint("generation_id", name="uq_turn_generation"),
        Index("ix_turn_conversation_state", "conversation_id", "state", "created_at"),
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
