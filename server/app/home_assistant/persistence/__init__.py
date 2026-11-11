"""home_assistant 领域 SQLAlchemy 映射；注册在共享 Base.metadata（迁移自 app.db.delivery）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Index,
    String,
    Text,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BIGINT_PK, Base


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
