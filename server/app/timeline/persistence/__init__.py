"""timeline 领域 SQLAlchemy 映射；注册在共享 Base.metadata（迁移自 app.db.memory）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BIGINT_PK, Base


class TimelineEventRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

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
