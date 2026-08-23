from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.chat import RuntimeActionCapability
from app.db import AppUserRecord, Database, DeviceClientRecord, DevicePairingCodeRecord
from app.ids import uuid7

DEVICE_ONLINE_WINDOW = timedelta(seconds=90)


class DeviceRegistryError(RuntimeError):
    pass


class DeviceNotFound(DeviceRegistryError):
    pass


class DeviceCredentialInvalid(DeviceRegistryError):
    pass


class PairingCodeInvalid(DeviceRegistryError):
    pass


class DeviceRevisionConflict(DeviceRegistryError):
    pass


class DeviceAliasConflict(DeviceRegistryError):
    pass


@dataclass(frozen=True, slots=True)
class DevicePrincipal:
    device_id: UUID
    owner_user_id: UUID


@dataclass(frozen=True, slots=True)
class PairingSecret:
    code: str
    expires_at: datetime
    owner_user_id: UUID
    granted_capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PairedDevice:
    access_token: str
    device: DeviceSnapshot


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    id: UUID
    owner_user_id: UUID
    name: str
    alias: str | None
    client_type: str
    capabilities: tuple[str, ...]
    granted_capabilities: tuple[str, ...]
    effective_capabilities: tuple[str, ...]
    revision: int
    paired_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None
    online: bool


class DeviceRegistry:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create_pairing_code(
        self,
        *,
        owner_user_id: UUID | None,
        granted_capabilities: tuple[str, ...],
        ttl_seconds: int = 600,
        actor: str = "admin",
    ) -> PairingSecret:
        now = datetime.now(UTC)
        async with self._database.sessions() as session:
            if owner_user_id is None:
                users = list(
                    await session.scalars(
                        select(AppUserRecord)
                        .where(AppUserRecord.status == "active")
                        .order_by(AppUserRecord.created_at)
                        .limit(2)
                    )
                )
                if len(users) != 1:
                    raise DeviceRegistryError(
                        "owner_user_id is required unless exactly one active user exists"
                    )
                owner_user_id = users[0].id
            else:
                user = await session.get(AppUserRecord, owner_user_id)
                if user is None or user.status != "active":
                    raise DeviceRegistryError("active owner user not found")

        code = f"aria_pair_{secrets.token_urlsafe(24)}"
        expires_at = now + timedelta(seconds=ttl_seconds)
        record = DevicePairingCodeRecord(
            id=uuid7(),
            owner_user_id=owner_user_id,
            code_hash=_hash_secret(code),
            granted_capabilities=list(_normalized_capabilities(granted_capabilities)),
            created_by=actor,
            created_at=now,
            expires_at=expires_at,
        )
        async with self._database.sessions.begin() as session:
            session.add(record)
        return PairingSecret(
            code=code,
            expires_at=expires_at,
            owner_user_id=owner_user_id,
            granted_capabilities=tuple(record.granted_capabilities),
        )

    async def pair(
        self,
        *,
        pairing_code: str,
        name: str,
        alias: str | None,
        client_type: str,
        capabilities: tuple[str, ...],
    ) -> PairedDevice:
        now = datetime.now(UTC)
        token = f"aria_device_{secrets.token_urlsafe(32)}"
        device_id = uuid7()
        try:
            async with self._database.sessions.begin() as session:
                pairing = await session.scalar(
                    select(DevicePairingCodeRecord)
                    .where(DevicePairingCodeRecord.code_hash == _hash_secret(pairing_code))
                    .limit(1)
                    .with_for_update()
                )
                if (
                    pairing is None
                    or pairing.claimed_at is not None
                    or _aware(pairing.expires_at) <= now
                ):
                    raise PairingCodeInvalid("pairing code is invalid, expired, or already used")
                record = DeviceClientRecord(
                    id=device_id,
                    owner_user_id=pairing.owner_user_id,
                    name=_clean_name(name),
                    alias=_clean_alias(alias),
                    client_type=client_type,
                    credential_hash=_hash_secret(token),
                    capabilities=list(_normalized_capabilities(capabilities)),
                    granted_capabilities=list(
                        _normalized_capabilities(tuple(pairing.granted_capabilities))
                    ),
                    revision=1,
                    paired_at=now,
                    last_seen_at=now,
                )
                session.add(record)
                await session.flush()
                pairing.claimed_at = now
                pairing.claimed_device_id = device_id
        except IntegrityError as error:
            raise DeviceAliasConflict("device alias is already in use for this owner") from error
        return PairedDevice(access_token=token, device=_snapshot(record, now=now))

    async def authenticate(self, access_token: str) -> DevicePrincipal:
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(DeviceClientRecord)
                .where(DeviceClientRecord.credential_hash == _hash_secret(access_token))
                .limit(1)
            )
        if record is None or record.revoked_at is not None:
            raise DeviceCredentialInvalid("invalid or revoked device credential")
        return DevicePrincipal(device_id=record.id, owner_user_id=record.owner_user_id)

    async def heartbeat(
        self,
        principal: DevicePrincipal,
        *,
        capabilities: tuple[str, ...],
    ) -> DeviceSnapshot:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(DeviceClientRecord, principal.device_id)
            if record is None or record.revoked_at is not None:
                raise DeviceCredentialInvalid("invalid or revoked device credential")
            normalized = list(_normalized_capabilities(capabilities))
            if record.capabilities != normalized:
                record.capabilities = normalized
                record.revision += 1
            record.last_seen_at = now
        return _snapshot(record, now=now)

    async def list_devices(self, *, owner_user_id: UUID | None = None) -> list[DeviceSnapshot]:
        query = select(DeviceClientRecord).order_by(DeviceClientRecord.paired_at.desc())
        if owner_user_id is not None:
            query = query.where(DeviceClientRecord.owner_user_id == owner_user_id)
        async with self._database.sessions() as session:
            records = list(await session.scalars(query))
        now = datetime.now(UTC)
        return [_snapshot(record, now=now) for record in records]

    async def get_device(self, device_id: UUID) -> DeviceSnapshot:
        async with self._database.sessions() as session:
            record = await session.get(DeviceClientRecord, device_id)
        if record is None:
            raise DeviceNotFound("device not found")
        return _snapshot(record, now=datetime.now(UTC))

    async def update_device(
        self,
        device_id: UUID,
        *,
        expected_revision: int,
        name: str | None = None,
        alias: str | None = None,
        alias_supplied: bool = False,
        granted_capabilities: tuple[str, ...] | None = None,
    ) -> DeviceSnapshot:
        now = datetime.now(UTC)
        try:
            async with self._database.sessions.begin() as session:
                record = await session.get(DeviceClientRecord, device_id, with_for_update=True)
                if record is None:
                    raise DeviceNotFound("device not found")
                if record.revision != expected_revision:
                    raise DeviceRevisionConflict("device revision changed")
                if name is not None:
                    record.name = _clean_name(name)
                if alias_supplied:
                    record.alias = _clean_alias(alias)
                if granted_capabilities is not None:
                    record.granted_capabilities = list(
                        _normalized_capabilities(granted_capabilities)
                    )
                record.revision += 1
        except IntegrityError as error:
            raise DeviceAliasConflict("device alias is already in use for this owner") from error
        return _snapshot(record, now=now)

    async def revoke(self, device_id: UUID) -> DeviceSnapshot:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(DeviceClientRecord, device_id, with_for_update=True)
            if record is None:
                raise DeviceNotFound("device not found")
            if record.revoked_at is None:
                record.revoked_at = now
                record.revision += 1
        return _snapshot(record, now=now)

    async def available_actions(self, user_id: UUID) -> tuple[RuntimeActionCapability, ...]:
        cutoff = datetime.now(UTC) - DEVICE_ONLINE_WINDOW
        async with self._database.sessions() as session:
            records = list(
                await session.scalars(
                    select(DeviceClientRecord).where(
                        DeviceClientRecord.owner_user_id == user_id,
                        DeviceClientRecord.revoked_at.is_(None),
                        DeviceClientRecord.last_seen_at >= cutoff,
                    )
                )
            )
        actions: list[RuntimeActionCapability] = []
        for record in records:
            granted = set(record.granted_capabilities)
            for capability in sorted(set(record.capabilities) & granted):
                actions.append(
                    RuntimeActionCapability(
                        capability_id=f"{record.id}:{capability}",
                        label=f"{record.alias or record.name} · {capability}",
                        description=f"在线设备 {record.name} 已授权的 {capability} 能力",
                    )
                )
        return tuple(actions)


def _snapshot(record: DeviceClientRecord, *, now: datetime) -> DeviceSnapshot:
    capabilities = tuple(sorted(set(record.capabilities)))
    granted = tuple(sorted(set(record.granted_capabilities)))
    effective = tuple(sorted(set(capabilities) & set(granted)))
    revoked_at = _aware(record.revoked_at) if record.revoked_at is not None else None
    last_seen_at = _aware(record.last_seen_at)
    return DeviceSnapshot(
        id=record.id,
        owner_user_id=record.owner_user_id,
        name=record.name,
        alias=record.alias,
        client_type=record.client_type,
        capabilities=capabilities,
        granted_capabilities=granted,
        effective_capabilities=effective,
        revision=record.revision,
        paired_at=_aware(record.paired_at),
        last_seen_at=last_seen_at,
        revoked_at=revoked_at,
        online=revoked_at is None and now - last_seen_at <= DEVICE_ONLINE_WINDOW,
    )


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _normalized_capabilities(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _clean_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise DeviceRegistryError("device name cannot be blank")
    return cleaned


def _clean_alias(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().casefold()
    return cleaned or None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
