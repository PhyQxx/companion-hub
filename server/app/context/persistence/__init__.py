"""context 领域 SQLAlchemy 映射；注册在共享 Base.metadata（迁移自 app.db.identity）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ObservationOwnerBindingRecord(Base):
    __module__ = "app.db.models"

    __tablename__ = "observation_owner_binding"
    __table_args__ = (CheckConstraint("slot = 1", name="ck_observation_owner_binding_slot"),)

    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Keep the minimal ownership evidence after account deletion. A foreign
    # key with cascade would silently enable reassignment of private sources.
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
