"""Domain ORM mappings: meetings; registered once on shared Base.metadata."""

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


class MeetingRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """MEET-01 会议会话：只保存授权后的文本转写，不保存原始音频。"""

    __tablename__ = "meeting"
    __table_args__ = (
        CheckConstraint(
            "status IN ('prepared','recording','consent_revoked','completed','cancelled')",
            name="ck_meeting_status",
        ),
        CheckConstraint(
            "privacy_level IN ('L1','L2')",
            name="ck_meeting_privacy_level",
        ),
        Index("ix_meeting_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    calendar_event_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("calendar_event.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(320), nullable=False)
    participants: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    briefing: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    privacy_level: Mapped[str] = mapped_column(String(2), nullable=False, default="L1")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="prepared")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transcript_segments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    summary: Mapped[str | None] = mapped_column(Text)
    decisions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    action_items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MeetingActionClaimRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """行动项确认的跨 worker 单次认领；未知结果禁止自动重试。"""

    __tablename__ = "meeting_action_claim"
    __table_args__ = (
        CheckConstraint(
            "status IN ('creating','created','unknown_outcome')",
            name="ck_meeting_action_claim_status",
        ),
        UniqueConstraint("meeting_id", "action_index", name="uq_meeting_action_claim"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    meeting_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("meeting.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    action_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="creating")
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task_item.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
