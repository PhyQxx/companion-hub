"""Content-free accounting retained independently of conversation deletion."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, ExactDecimal


class ModelCostRecord(Base):
    __tablename__ = "model_cost"
    __table_args__ = (
        CheckConstraint("state IN ('reserved','estimated','unknown')", name="ck_model_cost_state"),
        CheckConstraint(
            "charged_micros IS NULL OR charged_micros >= 0", name="ck_model_cost_charge"
        ),
        CheckConstraint(
            "unit IS NULL OR unit IN ('request','image','second','character','byte')",
            name="ck_model_cost_unit",
        ),
        CheckConstraint(
            "(unit IS NULL AND unit_rate IS NULL AND unit_maximum_quantity IS NULL "
            "AND unit_quantity IS NULL) OR (unit IS NOT NULL AND currency IS NOT NULL "
            "AND unit_rate IS NOT NULL AND unit_maximum_quantity IS NOT NULL "
            "AND input_rate IS NULL AND output_rate IS NULL)",
            name="ck_model_cost_unit_quote",
        ),
        CheckConstraint(
            "unit_rate IS NULL OR "
            "(CAST(unit_rate AS NUMERIC) >= 0 AND CAST(unit_rate AS NUMERIC) <= 1000000000)",
            name="ck_model_cost_unit_rate",
        ),
        CheckConstraint(
            "unit_maximum_quantity IS NULL OR "
            "(CAST(unit_maximum_quantity AS NUMERIC) > 0 "
            "AND CAST(unit_maximum_quantity AS NUMERIC) <= 1000000000)",
            name="ck_model_cost_unit_maximum",
        ),
        CheckConstraint(
            "unit_quantity IS NULL OR "
            "(CAST(unit_quantity AS NUMERIC) >= 0 "
            "AND CAST(unit_quantity AS NUMERIC) <= 1000000000)",
            name="ck_model_cost_unit_quantity",
        ),
        CheckConstraint(
            "unit IS NULL OR state != 'estimated' OR unit_quantity IS NOT NULL",
            name="ck_model_cost_unit_estimated",
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
    unit: Mapped[str | None] = mapped_column(String(16))
    unit_rate: Mapped[Decimal | None] = mapped_column(ExactDecimal())
    unit_maximum_quantity: Mapped[Decimal | None] = mapped_column(ExactDecimal())
    unit_quantity: Mapped[Decimal | None] = mapped_column(ExactDecimal())
    reserved_micros: Mapped[int | None] = mapped_column(BigInteger)
    charged_micros: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
