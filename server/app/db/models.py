from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
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


class DeviceClientRecord(Base):
    __tablename__ = "device_client"
    __table_args__ = (
        UniqueConstraint("credential_hash", name="uq_device_client_credential_hash"),
        # 别名只在活跃设备间唯一：撤销设备不释放记录但应让出别名，允许重新配对复用
        Index(
            "uq_device_client_owner_alias",
            "owner_user_id",
            "alias",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
        Index("ix_device_client_owner_seen", "owner_user_id", "revoked_at", "last_seen_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    alias: Mapped[str | None] = mapped_column(String(80))
    client_type: Mapped[str] = mapped_column(String(32), nullable=False)
    credential_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    granted_capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    paired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DevicePairingCodeRecord(Base):
    __tablename__ = "device_pairing_code"
    __table_args__ = (
        UniqueConstraint("code_hash", name="uq_device_pairing_code_hash"),
        Index("ix_device_pairing_code_expiry", "expires_at", "claimed_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    granted_capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_device_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("device_client.id", ondelete="SET NULL")
    )


class DeviceCommandRecord(Base):
    __tablename__ = "device_command"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','sent','acknowledged','succeeded','failed','cancelled',"
            "'expired','timed_out')",
            name="ck_device_command_status",
        ),
        UniqueConstraint("device_id", "idempotency_key", name="uq_device_command_idempotency"),
        Index("ix_device_command_status_expiry", "device_id", "status", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    device_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("device_client.id", ondelete="RESTRICT"), nullable=False
    )
    command_name: Mapped[str] = mapped_column(String(160), nullable=False)
    args_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_code: Mapped[str | None] = mapped_column(String(160))
    result_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON)


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
    last_turn_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MessageRecord(Base):
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


class HomeAssistantProactiveLogRecord(Base):
    __tablename__ = "home_assistant_proactive_log"
    __table_args__ = (
        Index("ix_ha_proactive_rule_created", "entity_id", "rule_id", "created_at"),
        Index("ix_ha_proactive_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(80), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    passed_gate: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=false())
    reason_code: Mapped[str | None] = mapped_column(String(160))
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProactiveDeliveryReceiptRecord(Base):
    __tablename__ = "proactive_delivery_receipt"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('web_chat','desktop_notification','voice')",
            name="ck_proactive_delivery_channel",
        ),
        CheckConstraint(
            "status IN ('delivered','failed')",
            name="ck_proactive_delivery_status",
        ),
        CheckConstraint(
            "privacy_level IN ('L0','L1','L2')",
            name="ck_proactive_delivery_privacy",
        ),
        Index("ix_proactive_delivery_user_created", "user_id", "created_at"),
        Index("ix_proactive_delivery_decision", "decision_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    decision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("cognitive_decision.id", ondelete="SET NULL"),
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(160))
    external_operation_id: Mapped[str | None] = mapped_column(String(160))
    privacy_level: Mapped[str] = mapped_column(String(2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CognitiveDecisionRecord(Base):
    __tablename__ = "cognitive_decision"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('ignore','record','inform','ask','suggest','act','escalate')",
            name="ck_cognitive_decision_kind",
        ),
        CheckConstraint(
            "urgency IN ('low','normal','high','critical')",
            name="ck_cognitive_decision_urgency",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_cognitive_decision_confidence",
        ),
        Index("ix_cognitive_user_created", "user_id", "created_at"),
        Index("ix_cognitive_trigger_created", "trigger_kind", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversation.id", ondelete="SET NULL")
    )
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(160), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    urgency: Mapped[str] = mapped_column(String(16), nullable=False)
    attention_score: Mapped[float] = mapped_column(Float, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    model_provider: Mapped[str | None] = mapped_column(String(80))
    model_name: Mapped[str | None] = mapped_column(String(200))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CognitiveGoalRecord(Base):
    __tablename__ = "cognitive_goal"
    __table_args__ = (
        CheckConstraint("kind IN ('user','shared','system')", name="ck_cognitive_goal_kind"),
        CheckConstraint(
            "status IN ('active','completed','cancelled','expired')",
            name="ck_cognitive_goal_status",
        ),
        Index("ix_cognitive_goal_user_status", "user_id", "status", "due_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # GOAL-01 提醒状态：pre_due/due 各提醒一次（时间戳非空即已提醒），
    # reminder_defer_until 为稍后/忽略降频的统一推迟闸门，ignored_count 记录忽略次数。
    pre_due_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_defer_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ignored_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CognitiveFeedbackRecord(Base):
    __tablename__ = "cognitive_feedback"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('accepted','ignored','snoozed','forbidden')",
            name="ck_cognitive_feedback_kind",
        ),
        Index("ix_cognitive_feedback_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    decision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("cognitive_decision.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SemanticEventAuditRecord(Base):
    __tablename__ = "semantic_event_audit"
    __table_args__ = (
        CheckConstraint(
            "disposition IN ('processed','suppressed','merged','expired','unstable')",
            name="ck_semantic_event_disposition",
        ),
        CheckConstraint(
            "privacy_level IN ('L0','L1','L2')",
            name="ck_semantic_event_privacy",
        ),
        Index("ix_semantic_event_user_created", "user_id", "created_at"),
        Index(
            "ix_semantic_event_user_dedupe_created",
            "user_id",
            "dedupe_key",
            "created_at",
        ),
    )

    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(160), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(240), nullable=False)
    privacy_level: Mapped[str] = mapped_column(String(2), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(160))
    decision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("cognitive_decision.id", ondelete="SET NULL"),
    )
    merged_into_event_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TimelineEventRecord(Base):
    __tablename__ = "timeline_event"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('message','event','device','tool','calendar','system')",
            name="ck_timeline_source_type",
        ),
        CheckConstraint(
            "actor IN ('user','assistant','device','system','external')",
            name="ck_timeline_actor",
        ),
        CheckConstraint(
            "privacy_level IN ('L0','L1','L2')",
            name="ck_timeline_privacy",
        ),
        CheckConstraint(
            "importance >= 0 AND importance <= 1",
            name="ck_timeline_importance",
        ),
        UniqueConstraint(
            "source_type",
            "source_id",
            "event_type",
            name="uq_timeline_source_event",
        ),
        Index("ix_timeline_user_occurred", "user_id", "occurred_at"),
        Index(
            "ix_timeline_user_event_occurred",
            "user_id",
            "event_type",
            "occurred_at",
        ),
        Index("ix_timeline_user_actor_occurred", "user_id", "actor", "occurred_at"),
        Index("ix_timeline_source", "source_type", "source_id"),
        Index("ix_timeline_conversation", "conversation_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    title: Mapped[str | None] = mapped_column(String(240))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    privacy_level: Mapped[str] = mapped_column(String(2), nullable=False)
    importance: Mapped[float] = mapped_column(nullable=False, default=0.3, server_default="0.3")
    entities: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    keywords: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    embedding: Mapped[list[float] | None] = mapped_column(JSON)
    embedding_model: Mapped[str | None] = mapped_column(String(160))
    embedding_version: Mapped[str | None] = mapped_column(String(64))
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
            "subject_kind IN ('user','assistant','shared')",
            name="ck_memory_subject_kind",
        ),
        CheckConstraint(
            "origin_kind IN ('user_statement','assistant_statement','shared_turn',"
            "'system_event','manual')",
            name="ck_memory_origin_kind",
        ),
        CheckConstraint(
            "status IN ('active','archived','superseded','conflict')",
            name="ck_memory_status",
        ),
        CheckConstraint("importance >= 0 AND importance <= 1", name="ck_memory_importance"),
        Index("ix_memory_user_type_status", "user_id", "type", "status", "importance"),
        Index(
            "ix_memory_user_subject_status",
            "user_id",
            "subject_kind",
            "subject_key",
            "status",
            "importance",
        ),
        Index(
            "ix_memory_fact_slot",
            "user_id",
            "subject_kind",
            "subject_key",
            "fact_key",
            "status",
        ),
        Index("ix_memory_superseded_by", "superseded_by"),
        Index("ix_memory_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    subject_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user"
    )
    subject_key: Mapped[str] = mapped_column(
        String(160), nullable=False, default="user:self", server_default="user:self"
    )
    fact_key: Mapped[str | None] = mapped_column(String(160))
    origin_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="user_statement", server_default="user_statement"
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


class DeletionLedgerRecord(Base):
    __tablename__ = "deletion_ledger"
    __table_args__ = (
        CheckConstraint(
            "entity_kind IN ('memory','message')", name="ck_deletion_ledger_entity_kind"
        ),
        Index("ix_deletion_ledger_created", "created_at"),
        Index("ix_deletion_ledger_entity", "entity_kind", "entity_id"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    entity_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # The ledger stores identifiers only — never the deleted content itself —
    # so a restored backup can replay deletions without resurrecting it.
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    deleted_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(400))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InteractionTurnRecord(Base):
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


class ActionPlanRecord(Base):
    __tablename__ = "action_plan"
    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_confirmation','ready','executing','completed',"
            "'partially_completed','failed','cancelled','expired')",
            name="ck_action_plan_status",
        ),
        CheckConstraint(
            "plan_kind IN ('standard','compensation')",
            name="ck_action_plan_kind",
        ),
        UniqueConstraint("user_id", "idempotency_key", name="uq_action_plan_user_idempotency"),
        Index("ix_action_plan_user_created", "user_id", "created_at"),
        Index("ix_action_plan_user_status", "user_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(240))
    plan_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, default="standard", server_default="standard"
    )
    source_plan_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("action_plan.id", ondelete="SET NULL")
    )
    undo_plan_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("action_plan.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(160))
    reason_code: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ActionStepRecord(Base):
    __tablename__ = "action_step"
    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_confirmation','ready','executing','completed','failed',"
            "'cancelled','skipped','unknown_outcome','expired')",
            name="ck_action_step_status",
        ),
        CheckConstraint(
            "verification_status IN ('pending','not_required','verified','inconclusive')",
            name="ck_action_step_verification_status",
        ),
        UniqueConstraint("plan_id", "position", name="uq_action_step_plan_position"),
        UniqueConstraint("idempotency_key", name="uq_action_step_idempotency"),
        Index("ix_action_step_plan_status", "plan_id", "status", "position"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    plan_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("action_plan.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    action_id: Mapped[str] = mapped_column(String(160), nullable=False)
    risk: Mapped[str] = mapped_column(String(4), nullable=False)
    confirmation_policy: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_arguments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    verification_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    verifier_id: Mapped[str | None] = mapped_column(String(160))
    compensation_action_id: Mapped[str | None] = mapped_column(String(160))
    compensates_step_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("action_step.id", ondelete="SET NULL")
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    verification_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    verification_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_code: Mapped[str | None] = mapped_column(String(160))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionResultRecord(Base):
    __tablename__ = "action_result"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('observed','prompted','blocked','executed','verified','unknown_outcome')",
            name="ck_action_result_outcome",
        ),
        Index("ix_action_result_user_created", "user_id", "created_at"),
        Index("ix_action_result_decision", "decision_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    decision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("cognitive_decision.id", ondelete="CASCADE"),
        nullable=False,
    )
    level: Mapped[str] = mapped_column(String(4), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(160))
    verified: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=false())
    observed_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReflectionCandidateRecord(Base):
    __tablename__ = "reflection_candidate"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','confirmed','rejected','superseded')",
            name="ck_reflection_candidate_status",
        ),
        Index("ix_reflection_candidate_user_created", "user_id", "created_at"),
        Index("ix_reflection_candidate_status", "user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    requires_confirmation: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default="true"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RuntimeLeaseRecord(Base):
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


class JobRecord(Base):
    __tablename__ = "job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','admitted','running','waiting_user','retry_wait',"
            "'cancelling','succeeded','failed','cancelled')",
            name="ck_job_status",
        ),
        Index("ix_job_status_available", "status", "available_at"),
        Index("ix_job_owner_created", "owner", "created_at"),
        Index("ix_job_idempotency", "idempotency_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[str] = mapped_column(String(160), nullable=False, server_default="local-user")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="50")
    idempotency_key: Mapped[str | None] = mapped_column(String(256), unique=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    progress: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    current_step: Mapped[str | None] = mapped_column(String(128))
    resource_class: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="cpu-small"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(160))
    error_detail_safe: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobStepRecord(Base):
    __tablename__ = "job_step"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_job_step_status",
        ),
        UniqueConstraint("job_id", "name", "attempt", name="uq_job_step_name_attempt"),
        Index("ix_job_step_job", "job_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    progress: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class JobArtifactRecord(Base):
    __tablename__ = "job_artifact"
    __table_args__ = (Index("ix_job_artifact_asset", "asset_id"),)

    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job.id", ondelete="CASCADE"), primary_key=True
    )
    step_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    asset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    committed: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AssetRecord(Base):
    __tablename__ = "asset"
    __table_args__ = (
        CheckConstraint(
            "state IN ('staging','active','unreferenced','deleting','deleted','quarantined')",
            name="ck_asset_state",
        ),
        CheckConstraint("privacy_level IN ('L0','L1','L2')", name="ck_asset_privacy_level"),
        Index("ix_asset_hash", "content_hash"),
        Index("ix_asset_state", "state", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    privacy_level: Mapped[str] = mapped_column(String(4), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="staging")
    encryption_key_ref: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetReferenceRecord(Base):
    __tablename__ = "asset_reference"

    asset_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("asset.id", ondelete="CASCADE"), primary_key=True
    )
    owner_kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AssetDerivationRecord(Base):
    __tablename__ = "asset_derivation"

    parent_asset_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("asset.id", ondelete="CASCADE"), primary_key=True
    )
    child_asset_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("asset.id", ondelete="CASCADE"), primary_key=True
    )
    operation: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_version: Mapped[str | None] = mapped_column(String(128))
    params_hash: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AvatarPackRecord(Base):
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
    __tablename__ = "ui_preference"
    __table_args__ = (
        CheckConstraint(
            "appearance_mode IN ('light','dark','system')",
            name="ck_ui_preference_mode",
        ),
    )

    owner: Mapped[str] = mapped_column(String(160), primary_key=True)
    theme_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ui_theme.id", ondelete="RESTRICT"), nullable=False
    )
    appearance_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="light")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TaskItemRecord(Base):
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
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fire_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_delivery: Mapped[dict[str, Any] | None] = mapped_column(JSON)
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
    channels: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
