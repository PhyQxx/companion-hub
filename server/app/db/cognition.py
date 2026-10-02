"""Domain ORM mappings: cognition; registered once on shared Base.metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class CognitiveDecisionRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

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
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "cognitive_goal"
    __table_args__ = (
        CheckConstraint("kind IN ('user','shared','system')", name="ck_cognitive_goal_kind"),
        CheckConstraint(
            "status IN ('active','completed','cancelled','expired')",
            name="ck_cognitive_goal_status",
        ),
        CheckConstraint(
            "privacy_level IS NULL OR privacy_level IN ('L0','L1','L2')",
            name="ck_cognitive_goal_privacy",
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
    privacy_level: Mapped[str | None] = mapped_column(String(2))
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
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

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
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

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


class ActionResultRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

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
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

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
