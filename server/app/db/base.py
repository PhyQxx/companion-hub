"""Shared ORM identity; domain mapping moves must keep this metadata object."""

from sqlalchemy import BigInteger, Integer
from sqlalchemy.orm import DeclarativeBase

BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass
