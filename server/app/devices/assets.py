from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.ids import uuid7

DEVICE_ASSET_TTL = timedelta(minutes=2)
MAX_DEVICE_ASSET_BYTES = 8 * 1024 * 1024
MAX_DEVICE_ASSETS = 32


class EphemeralDeviceAssetError(RuntimeError):
    pass


class EphemeralDeviceAssetNotFound(EphemeralDeviceAssetError):
    pass


@dataclass(frozen=True, slots=True)
class EphemeralDeviceAsset:
    id: UUID
    owner_user_id: UUID
    device_id: UUID
    command_id: UUID
    media_type: str
    data: bytes
    sha256: str
    created_at: datetime
    expires_at: datetime


class EphemeralDeviceAssetStore:
    """Process-local, bounded, consume-on-read storage for sensitive device media."""

    def __init__(self) -> None:
        self._assets: dict[UUID, EphemeralDeviceAsset] = {}
        self._command_assets: dict[UUID, UUID] = {}
        self._lock = asyncio.Lock()

    async def put(
        self,
        *,
        owner_user_id: UUID,
        device_id: UUID,
        command_id: UUID,
        media_type: str,
        data: bytes,
        now: datetime | None = None,
    ) -> EphemeralDeviceAsset:
        _validate_asset(media_type, data)
        created_at = now or datetime.now(UTC)
        asset = EphemeralDeviceAsset(
            id=uuid7(),
            owner_user_id=owner_user_id,
            device_id=device_id,
            command_id=command_id,
            media_type=media_type,
            data=data,
            sha256=hashlib.sha256(data).hexdigest(),
            created_at=created_at,
            expires_at=created_at + DEVICE_ASSET_TTL,
        )
        async with self._lock:
            self._prune(created_at)
            previous_id = self._command_assets.get(command_id)
            if previous_id is not None:
                self._assets.pop(previous_id, None)
            self._assets[asset.id] = asset
            self._command_assets[command_id] = asset.id
            while len(self._assets) > MAX_DEVICE_ASSETS:
                oldest = min(self._assets.values(), key=lambda item: item.created_at)
                self._remove(oldest.id)
        return asset

    async def consume(
        self,
        asset_id: UUID,
        *,
        owner_user_id: UUID,
        command_id: UUID,
        now: datetime | None = None,
    ) -> EphemeralDeviceAsset:
        checked_at = now or datetime.now(UTC)
        async with self._lock:
            self._prune(checked_at)
            asset = self._assets.get(asset_id)
            if (
                asset is None
                or asset.owner_user_id != owner_user_id
                or asset.command_id != command_id
            ):
                raise EphemeralDeviceAssetNotFound("ephemeral device asset not found")
            self._remove(asset.id)
        return asset

    def _prune(self, now: datetime) -> None:
        for asset in tuple(self._assets.values()):
            if asset.expires_at <= now:
                self._remove(asset.id)

    def _remove(self, asset_id: UUID) -> None:
        asset = self._assets.pop(asset_id, None)
        if asset is not None and self._command_assets.get(asset.command_id) == asset_id:
            self._command_assets.pop(asset.command_id, None)


def _validate_asset(media_type: str, data: bytes) -> None:
    if not data:
        raise EphemeralDeviceAssetError("device asset is empty")
    if len(data) > MAX_DEVICE_ASSET_BYTES:
        raise EphemeralDeviceAssetError("device asset exceeds size limit")
    signatures = {
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/png": (b"\x89PNG\r\n\x1a\n",),
    }
    prefixes = signatures.get(media_type)
    if prefixes is None or not data.startswith(prefixes):
        raise EphemeralDeviceAssetError("unsupported or invalid device asset")
