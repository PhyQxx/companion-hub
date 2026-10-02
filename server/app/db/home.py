"""Domain ORM mappings: home; registered once on shared Base.metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class HomeSceneRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """HOME-01 家庭场景：感知事件触发 → 展开为待确认行动计划。

    steps 是 ActionInvocation 形状的有序列表（动作须在 Action Registry
    注册）；计划—确认—执行由 ActionPlanService 全权把关，场景本身
    不直接执行任何设备动作。trigger 与 Perception 管线事件 kind 精确
    匹配（如 user_arrived_home/user_left_home），manual 只能由用户
    显式运行。
    """

    __tablename__ = "home_scene"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_home_scene_user_name"),
        Index("ix_home_scene_user_trigger", "user_id", "trigger"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    trigger: Mapped[str] = mapped_column(String(120), nullable=False)
    # 场景生效时段（本地时间；支持跨夜如 21:00-07:00）；NULL 表示全天
    window_start: Mapped[str | None] = mapped_column(String(5))
    window_end: Mapped[str | None] = mapped_column(String(5))
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SafetyAlertRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """SAFE-02 告警状态机：critical 规则的 L1→L2 升级与 ack 终态。

    L3（预授权联系人邮件）在 S3 接入前不落 level；evidence 只存确定性
    字段（来源实体/持续/置信度），不含 L2 内容。
    """

    __tablename__ = "safety_alert"
    __table_args__ = (
        CheckConstraint(
            "status IN ('escalating','acknowledged','expired')",
            name="ck_safety_alert_status",
        ),
        CheckConstraint("severity IN ('critical')", name="ck_safety_alert_severity"),
        CheckConstraint("level IN (1, 2)", name="ck_safety_alert_level"),
        Index("ix_safety_alert_user_status_created", "user_id", "status", "created_at"),
        Index(
            "ix_safety_alert_active_dedupe",
            "user_id",
            "entity_id",
            "rule_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[str] = mapped_column(String(160), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(12), nullable=False, default="critical")
    message: Mapped[str] = mapped_column(String(2_000), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="escalating")
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    l1_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    l2_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ack_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SafetyAuthorizationRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """SAFE-02 预授权：危急告警升级到第三方联系人的显式许可（可撤销）。

    destination 在授权时由用户显式提供并落库；授权记录本身即是同意凭证，
    不依赖联系人的自由偏好字段。
    """

    __tablename__ = "safety_authorization"
    __table_args__ = (
        CheckConstraint("status IN ('active','revoked')", name="ck_safety_authorization_status"),
        CheckConstraint("channel IN ('email')", name="ck_safety_authorization_channel"),
        Index("ix_safety_authorization_user_status", "user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    contact_name: Mapped[str] = mapped_column(String(120), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="email")
    destination: Mapped[str] = mapped_column(String(254), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SafetyAlertEscalationRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """SAFE-02 升级台账：每次 L3 第三方联系动作的发送结果审计。"""

    __tablename__ = "safety_alert_escalation"
    __table_args__ = (
        CheckConstraint(
            "status IN ('sent','failed','skipped')",
            name="ck_safety_alert_escalation_status",
        ),
        CheckConstraint("level IN (3)", name="ck_safety_alert_escalation_level"),
        Index("ix_safety_alert_escalation_alert", "alert_id", "level"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    alert_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("safety_alert.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    destination: Mapped[str] = mapped_column(String(254), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
