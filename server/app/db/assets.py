"""Domain ORM mappings: assets; registered once on shared Base.metadata."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AssetRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "asset"
    __table_args__ = (
        CheckConstraint(
            "state IN ('staging','active','unreferenced','deleting','deleted','quarantined')",
            name="ck_asset_state",
        ),
        CheckConstraint("privacy_level IN ('L0','L1','L2')", name="ck_asset_privacy_level"),
        Index("ix_asset_hash", "content_hash"),
        Index("ix_asset_state", "state", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    privacy_level: Mapped[str] = mapped_column(String(4), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="staging")
    encryption_key_ref: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetReferenceRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "asset_reference"

    asset_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("asset.id", ondelete="CASCADE"), primary_key=True
    )
    owner_kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AssetDerivationRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    __tablename__ = "asset_derivation"

    parent_asset_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("asset.id", ondelete="CASCADE"), primary_key=True
    )
    child_asset_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("asset.id", ondelete="CASCADE"), primary_key=True
    )
    operation: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_version: Mapped[str | None] = mapped_column(String(128))
    params_hash: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
