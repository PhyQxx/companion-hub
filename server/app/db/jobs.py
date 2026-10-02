"""Domain ORM mappings: jobs; registered once on shared Base.metadata."""

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
    UniqueConstraint,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import BIGINT_PK, Base


class JobRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

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

    task_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_run.id", ondelete="SET NULL"),
        index=True,
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
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

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
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

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
