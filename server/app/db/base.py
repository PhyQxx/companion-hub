"""Shared ORM identity; domain mapping moves must keep this metadata object."""

from decimal import Decimal
from typing import cast

from sqlalchemy import BigInteger, Integer, Numeric, String
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator, TypeEngine

BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


class ExactDecimal(TypeDecorator[Decimal]):
    """SQLite NUMERIC binds a float; unit quotes must keep every declared digit."""

    impl = Numeric(24, 12)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[object]:
        implementation = String(64) if dialect.name == "sqlite" else Numeric(24, 12)
        return dialect.type_descriptor(cast(TypeEngine[object], implementation))

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> Decimal | str | None:
        return str(value) if value is not None and dialect.name == "sqlite" else value

    def process_result_value(self, value: Decimal | str | None, dialect: Dialect) -> Decimal | None:
        return Decimal(value) if value is not None else None
