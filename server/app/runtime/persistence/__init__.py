"""runtime 领域 SQLAlchemy 映射；注册在共享 Base.metadata（迁移自 app.db.conversation）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BIGINT_PK, Base


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
