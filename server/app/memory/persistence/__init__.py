"""memory 领域 SQLAlchemy 映射；注册在共享 Base.metadata（迁移自 app.db.memory）。"""

from __future__ import annotations

from datetime import datetime
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
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BIGINT_PK, Base


class MemoryRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

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
    tier: Mapped[str] = mapped_column(
        String(16), nullable=False, default="medium", server_default="medium"
    )
    tier_locked: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=false())
    tier_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_decay_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    lifecycle_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    access_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class MemorySourceRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

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
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

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


class MemoryUsageRecord(Base):
    __module__ = "app.db.models"
    __tablename__ = "memory_usage"
    __table_args__ = (Index("uq_memory_usage_turn", "memory_id", "turn_key", unique=True),)
    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    memory_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("memory.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    turn_key: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    day: Mapped[str] = mapped_column(String(10), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryLifecycleRecord(Base):
    __module__ = "app.db.models"
    __tablename__ = "memory_lifecycle_event"
    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    memory_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("memory.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    old_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    new_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
