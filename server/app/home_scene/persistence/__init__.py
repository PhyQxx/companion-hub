"""home_scene 领域 SQLAlchemy 映射；注册在共享 Base.metadata（迁移自 app.db.home）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


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
