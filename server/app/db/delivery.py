"""Domain ORM mappings: delivery; registered once on shared Base.metadata."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import BIGINT_PK, Base


class HomeAssistantProactiveLogRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

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


class PushSubscriptionRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """Web Push 订阅：浏览器 PushSubscription 的服务端镜像。

    endpoint 是推送服务分配的全局唯一 URL，按它 upsert；同一浏览器换账号
    登录时重绑 user_id。推送服务返回 404/410 表示订阅失效，立即删除。
    """

    __tablename__ = "push_subscription"
    __table_args__ = (
        CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_push_subscription_failures",
        ),
        Index("ix_push_subscription_user", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(String(768), nullable=False, unique=True)
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(255), nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(nullable=False, default=0)
    last_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
