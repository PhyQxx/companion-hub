"""output 领域 SQLAlchemy 映射；注册在共享 Base.metadata（迁移自 app.db.delivery）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProactiveDeliveryReceiptRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "proactive_delivery_receipt"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('web_chat','desktop_notification','web_push','voice')",
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
