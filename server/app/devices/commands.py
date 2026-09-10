from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db import Database, DeviceClientRecord, DeviceCommandRecord
from app.ids import uuid7
from app.observability import redact_fields

CommandStatus = Literal[
    "pending",
    "sent",
    "acknowledged",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
    "timed_out",
]
TERMINAL_COMMAND_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "expired", "timed_out"}
)


class DeviceCommandError(RuntimeError):
    pass


class DeviceCommandNotFound(DeviceCommandError):
    pass


class DeviceCommandConflict(DeviceCommandError):
    pass


@dataclass(frozen=True, slots=True)
class CommandSnapshot:
    id: UUID
    device_id: UUID
    command_name: str
    args_redacted: dict[str, Any]
    idempotency_key: str
    status: CommandStatus
    revision: int
    issued_at: datetime
    expires_at: datetime
    sent_at: datetime | None
    acknowledged_at: datetime | None
    completed_at: datetime | None
    reason_code: str | None
    result_meta: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class IssuedCommand:
    command: CommandSnapshot
    created: bool


class DeviceCommandStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(
        self,
        *,
        device_id: UUID,
        command_name: str,
        args: dict[str, Any],
        idempotency_key: str,
        ttl_seconds: int,
    ) -> IssuedCommand:
        existing = await self._find_idempotent(device_id, idempotency_key)
        if existing is not None:
            self._validate_idempotent_request(existing, command_name, args)
            return IssuedCommand(command=existing, created=False)
        now = datetime.now(UTC)
        record = DeviceCommandRecord(
            id=uuid7(),
            device_id=device_id,
            command_name=command_name,
            args_redacted=redact_fields(args),
            idempotency_key=idempotency_key,
            status="pending",
            revision=1,
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        try:
            async with self._database.sessions.begin() as session:
                device = await session.get(DeviceClientRecord, device_id)
                if device is None or device.revoked_at is not None:
                    raise DeviceCommandNotFound("active device not found")
                session.add(record)
        except IntegrityError:
            existing = await self._find_idempotent(device_id, idempotency_key)
            if existing is None:
                raise
            self._validate_idempotent_request(existing, command_name, args)
            return IssuedCommand(command=existing, created=False)
        return IssuedCommand(command=_snapshot(record), created=True)

    async def mark_sent(self, command_id: UUID) -> CommandSnapshot:
        return await self._transition(
            command_id,
            allowed={"pending"},
            status="sent",
            timestamp_field="sent_at",
        )

    async def acknowledge(self, device_id: UUID, command_id: UUID) -> CommandSnapshot:
        return await self._transition(
            command_id,
            device_id=device_id,
            allowed={"sent", "acknowledged"},
            status="acknowledged",
            timestamp_field="acknowledged_at",
            idempotent_status="acknowledged",
        )

    async def complete(
        self,
        device_id: UUID,
        command_id: UUID,
        *,
        outcome: Literal["succeeded", "failed"],
        reason_code: str | None,
        result_meta: dict[str, Any] | None,
    ) -> CommandSnapshot:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(DeviceCommandRecord, command_id, with_for_update=True)
            self._validate_target(record, device_id)
            assert record is not None
            if record.status in TERMINAL_COMMAND_STATUSES:
                return _snapshot(record)
            if _aware(record.expires_at) <= now:
                record.status = "timed_out" if record.sent_at else "expired"
                record.reason_code = "command_timeout"
            elif record.status not in {"sent", "acknowledged"}:
                raise DeviceCommandConflict(f"cannot complete command from {record.status}")
            else:
                record.status = outcome
                record.reason_code = reason_code
                record.result_meta = (
                    redact_fields(result_meta) if result_meta is not None else None
                )
            record.completed_at = now
            record.revision += 1
        return _snapshot(record)

    async def cancel(self, command_id: UUID) -> CommandSnapshot:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(DeviceCommandRecord, command_id, with_for_update=True)
            if record is None:
                raise DeviceCommandNotFound("device command not found")
            if record.status not in TERMINAL_COMMAND_STATUSES:
                record.status = "cancelled"
                record.reason_code = "cancelled_by_admin"
                record.completed_at = now
                record.revision += 1
        return _snapshot(record)

    async def mark_delivery_failed(
        self, command_id: UUID, *, reason_code: str
    ) -> CommandSnapshot:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(DeviceCommandRecord, command_id, with_for_update=True)
            if record is None:
                raise DeviceCommandNotFound("device command not found")
            if record.status not in TERMINAL_COMMAND_STATUSES:
                record.status = "failed"
                record.reason_code = reason_code
                record.completed_at = now
                record.revision += 1
        return _snapshot(record)

    async def mark_timeout(self, command_id: UUID) -> CommandSnapshot:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(DeviceCommandRecord, command_id, with_for_update=True)
            if record is None:
                raise DeviceCommandNotFound("device command not found")
            if record.status not in TERMINAL_COMMAND_STATUSES and _aware(record.expires_at) <= now:
                record.status = "timed_out" if record.sent_at else "expired"
                record.reason_code = "command_timeout"
                record.completed_at = now
                record.revision += 1
        return _snapshot(record)

    async def get(self, command_id: UUID) -> CommandSnapshot:
        await self.mark_timeout(command_id)
        async with self._database.sessions() as session:
            record = await session.get(DeviceCommandRecord, command_id)
        if record is None:
            raise DeviceCommandNotFound("device command not found")
        return _snapshot(record)

    async def list(
        self,
        *,
        device_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
        exclude_idempotency_prefixes: Sequence[str] = (),
    ) -> list[CommandSnapshot]:
        query = (
            select(DeviceCommandRecord)
            .order_by(DeviceCommandRecord.issued_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if device_id is not None:
            query = query.where(DeviceCommandRecord.device_id == device_id)
        query = _exclude_idempotency_prefixes(query, exclude_idempotency_prefixes)
        async with self._database.sessions() as session:
            records = list(await session.scalars(query))
        results: list[CommandSnapshot] = []
        now = datetime.now(UTC)
        for record in records:
            if record.status not in TERMINAL_COMMAND_STATUSES and _aware(record.expires_at) <= now:
                results.append(await self.mark_timeout(record.id))
            else:
                results.append(_snapshot(record))
        return results

    async def count(
        self,
        *,
        device_id: UUID | None = None,
        exclude_idempotency_prefixes: Sequence[str] = (),
    ) -> int:
        query = select(func.count()).select_from(DeviceCommandRecord)
        if device_id is not None:
            query = query.where(DeviceCommandRecord.device_id == device_id)
        query = _exclude_idempotency_prefixes(query, exclude_idempotency_prefixes)
        async with self._database.sessions() as session:
            return int(await session.scalar(query) or 0)

    async def _find_idempotent(
        self, device_id: UUID, idempotency_key: str
    ) -> CommandSnapshot | None:
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(DeviceCommandRecord)
                .where(
                    DeviceCommandRecord.device_id == device_id,
                    DeviceCommandRecord.idempotency_key == idempotency_key,
                )
                .limit(1)
            )
        return _snapshot(record) if record is not None else None

    async def _transition(
        self,
        command_id: UUID,
        *,
        allowed: set[str],
        status: CommandStatus,
        timestamp_field: str,
        device_id: UUID | None = None,
        idempotent_status: str | None = None,
    ) -> CommandSnapshot:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(DeviceCommandRecord, command_id, with_for_update=True)
            self._validate_target(record, device_id)
            assert record is not None
            if idempotent_status is not None and record.status == idempotent_status:
                return _snapshot(record)
            if record.status not in allowed:
                raise DeviceCommandConflict(f"cannot transition command from {record.status}")
            if _aware(record.expires_at) <= now:
                record.status = "timed_out" if record.sent_at else "expired"
                record.reason_code = "command_timeout"
                record.completed_at = now
            else:
                record.status = status
                setattr(record, timestamp_field, now)
            record.revision += 1
        return _snapshot(record)

    @staticmethod
    def _validate_target(
        record: DeviceCommandRecord | None, device_id: UUID | None
    ) -> None:
        if record is None or (device_id is not None and record.device_id != device_id):
            raise DeviceCommandNotFound("device command not found")

    @staticmethod
    def _validate_idempotent_request(
        existing: CommandSnapshot,
        command_name: str,
        args: dict[str, Any],
    ) -> None:
        if (
            existing.command_name != command_name
            or existing.args_redacted != redact_fields(args)
        ):
            raise DeviceCommandConflict(
                "idempotency key was already used with a different command request"
            )


def _exclude_idempotency_prefixes(query: Any, prefixes: Sequence[str]) -> Any:
    """按幂等键前缀排除感知循环的轮询命令，list/count 保持同一条件。"""
    for prefix in prefixes:
        query = query.where(~DeviceCommandRecord.idempotency_key.startswith(prefix))
    return query


def _snapshot(record: DeviceCommandRecord) -> CommandSnapshot:
    return CommandSnapshot(
        id=record.id,
        device_id=record.device_id,
        command_name=record.command_name,
        args_redacted=dict(record.args_redacted),
        idempotency_key=record.idempotency_key,
        status=cast(CommandStatus, record.status),
        revision=record.revision,
        issued_at=_aware(record.issued_at),
        expires_at=_aware(record.expires_at),
        sent_at=_aware(record.sent_at) if record.sent_at else None,
        acknowledged_at=_aware(record.acknowledged_at) if record.acknowledged_at else None,
        completed_at=_aware(record.completed_at) if record.completed_at else None,
        reason_code=record.reason_code,
        result_meta=dict(record.result_meta) if record.result_meta is not None else None,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
