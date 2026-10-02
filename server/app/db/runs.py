"""Domain ORM mappings: runs; registered once on shared Base.metadata."""

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
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TaskRunRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "task_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('accepted','running','succeeded','failed','cancelled')",
            name="ck_task_run_status",
        ),
        CheckConstraint("privacy_level IN ('L0','L1','L2')", name="ck_task_run_privacy"),
        UniqueConstraint("user_id", "request_id", name="uq_task_run_user_request"),
        Index("ix_task_run_user_created", "user_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversation.id", ondelete="CASCADE")
    )
    parent_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task_run.id", ondelete="SET NULL")
    )
    request_id: Mapped[str | None] = mapped_column(String(160))
    contract: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    privacy_level: Mapped[str] = mapped_column(String(2), nullable=False)
    config_version: Mapped[int | None] = mapped_column(Integer)
    persona_version: Mapped[int | None] = mapped_column(Integer)
    budget: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    llm_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    budget_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskRunEventRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "task_run_event"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_task_run_event_seq"),)
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task_run.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    privacy_level: Mapped[str] = mapped_column(String(2), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModelReservationRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "model_reservation"
    __table_args__ = (
        CheckConstraint(
            "state IN ('reserved','settled','unknown')", name="ck_model_reservation_state"
        ),
        CheckConstraint(
            "reserved_tokens > 0 AND charged_tokens >= 0", name="ck_model_reservation_tokens"
        ),
        CheckConstraint(
            "phase IN ('interactive','maintenance')", name="ck_model_reservation_phase"
        ),
        Index("ix_model_reservation_run_created", "run_id", "created_at"),
    )
    call_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task_run.id", ondelete="CASCADE"), nullable=False
    )
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(160), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charged_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_tokens: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
