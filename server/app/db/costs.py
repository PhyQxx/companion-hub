"""Content-free accounting retained independently of conversation deletion."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ModelCostRecord(Base):
    __tablename__ = "model_cost"
    __table_args__ = (
        CheckConstraint("state IN ('reserved','estimated','unknown')", name="ck_model_cost_state"),
        CheckConstraint(
            "charged_micros IS NULL OR charged_micros >= 0", name="ck_model_cost_charge"
        ),
        Index("ix_model_cost_user_created", "user_id", "created_at"),
    )
    # No source FK: forgetting content must not reset a user's recorded spending.
    call_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(160), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3))
    input_rate: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))
    output_rate: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))
    reserved_micros: Mapped[int | None] = mapped_column(BigInteger)
    charged_micros: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
