from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.db import ConfigPointerRecord, ConfigVersionRecord, Database

from .models import HubConfig
from .store import (
    ConfigAudit,
    ConfigSnapshot,
    ConfigValidator,
    hash_config,
    load_config_file,
)


@dataclass(frozen=True, slots=True)
class DatabaseConfigVersion:
    version: int
    status: str
    content_hash: str
    config: HubConfig
    created_by: str
    created_at: datetime
    published_at: datetime | None
    rollback_from_version: int | None


class DatabaseConfigStore:
    """Immutable database versions with an atomically switched current pointer."""

    def __init__(
        self,
        database: Database,
        bootstrap_path: Path,
        *,
        validators: tuple[ConfigValidator, ...] = (),
    ) -> None:
        self._database = database
        self._bootstrap_path = bootstrap_path
        self._validators = validators
        self._lock = asyncio.Lock()
        self._current: ConfigSnapshot | None = None
        self.audit: list[ConfigAudit] = []
        self.last_error: str | None = None

    @property
    def current(self) -> ConfigSnapshot:
        if self._current is None:
            raise RuntimeError("configuration has not been loaded")
        return self._current

    async def load(self) -> ConfigSnapshot:
        async with self._lock:
            existing = await self._read_current()
            if existing is None:
                candidate, content_hash = load_config_file(self._bootstrap_path)
                await self._validate(candidate)
                now = datetime.now(UTC)
                async with self._database.sessions.begin() as session:
                    record = ConfigVersionRecord(
                        status="published",
                        content=candidate.model_dump(mode="json"),
                        content_hash=content_hash,
                        created_by="bootstrap",
                        published_at=now,
                    )
                    session.add(record)
                    await session.flush()
                    session.add(ConfigPointerRecord(id=1, current_version_id=record.id))
                existing = self._version_from_record(record)
            self._current = self._snapshot(existing)
            self.last_error = None
            return self._current

    async def refresh(self) -> ConfigSnapshot:
        async with self._lock:
            current = await self._read_current()
            if current is None:
                raise RuntimeError("database configuration pointer is missing")
            self._current = self._snapshot(current)
            return self._current

    async def validate(self, config: HubConfig) -> None:
        await self._validate(config)

    async def create_draft(
        self,
        config: HubConfig,
        *,
        actor: str,
    ) -> DatabaseConfigVersion:
        await self._validate_with_audit(config)
        async with self._database.sessions.begin() as session:
            record = ConfigVersionRecord(
                status="draft",
                content=config.model_dump(mode="json"),
                content_hash=hash_config(config),
                created_by=actor,
            )
            session.add(record)
            await session.flush()
            await session.refresh(record)
        result = self._version_from_record(record)
        self.audit.append(ConfigAudit("draft", True, result.version, datetime.now(UTC)))
        return result

    async def publish(self, version: int, *, actor: str) -> ConfigSnapshot:
        del actor  # Authentication identity is recorded when the immutable draft is created.
        async with self._lock:
            candidate = await self.get_version(version)
            if candidate.status != "draft":
                raise ValueError("only a draft configuration can be published")
            await self._validate_with_audit(candidate.config)
            now = datetime.now(UTC)
            async with self._database.sessions.begin() as session:
                pointer = await session.scalar(
                    select(ConfigPointerRecord)
                    .where(ConfigPointerRecord.id == 1)
                    .with_for_update()
                )
                if pointer is None:
                    raise RuntimeError("database configuration pointer is missing")
                current = await session.get(ConfigVersionRecord, pointer.current_version_id)
                target = await session.get(ConfigVersionRecord, version)
                if current is None or target is None or target.status != "draft":
                    raise ValueError("configuration draft is no longer publishable")
                current.status = "superseded"
                target.status = "published"
                target.published_at = now
                pointer.current_version_id = target.id
            published = DatabaseConfigVersion(
                version=candidate.version,
                status="published",
                content_hash=candidate.content_hash,
                config=candidate.config,
                created_by=candidate.created_by,
                created_at=candidate.created_at,
                published_at=now,
                rollback_from_version=candidate.rollback_from_version,
            )
            self._current = self._snapshot(published)
            self.last_error = None
            self.audit.append(ConfigAudit("publish", True, version, now))
            return self._current

    async def rollback(self, version: int, *, actor: str) -> ConfigSnapshot:
        async with self._lock:
            target = await self.get_version(version)
            if target.status == "draft":
                raise ValueError("a draft cannot be used as a rollback target")
            await self._validate_with_audit(target.config)
            now = datetime.now(UTC)
            async with self._database.sessions.begin() as session:
                pointer = await session.scalar(
                    select(ConfigPointerRecord)
                    .where(ConfigPointerRecord.id == 1)
                    .with_for_update()
                )
                if pointer is None:
                    raise RuntimeError("database configuration pointer is missing")
                current = await session.get(ConfigVersionRecord, pointer.current_version_id)
                if current is None:
                    raise RuntimeError("published configuration is missing")
                current.status = "superseded"
                restored = ConfigVersionRecord(
                    status="published",
                    content=target.config.model_dump(mode="json"),
                    content_hash=target.content_hash,
                    created_by=actor,
                    published_at=now,
                    rollback_from_version=target.version,
                )
                session.add(restored)
                await session.flush()
                pointer.current_version_id = restored.id
                await session.refresh(restored)
            published = self._version_from_record(restored)
            self._current = self._snapshot(published)
            self.last_error = None
            self.audit.append(ConfigAudit("rollback", True, published.version, now))
            return self._current

    async def list_versions(self, *, limit: int = 50) -> list[DatabaseConfigVersion]:
        async with self._database.sessions() as session:
            records = await session.scalars(
                select(ConfigVersionRecord)
                .order_by(ConfigVersionRecord.id.desc())
                .limit(limit)
            )
            return [self._version_from_record(record) for record in records]

    async def get_version(self, version: int) -> DatabaseConfigVersion:
        async with self._database.sessions() as session:
            record = await session.get(ConfigVersionRecord, version)
        if record is None:
            raise LookupError(f"configuration version not found: {version}")
        return self._version_from_record(record)

    async def _read_current(self) -> DatabaseConfigVersion | None:
        async with self._database.sessions() as session:
            pointer = await session.get(ConfigPointerRecord, 1)
            if pointer is None:
                return None
            record = await session.get(ConfigVersionRecord, pointer.current_version_id)
        if record is None:
            raise RuntimeError("published configuration is missing")
        return self._version_from_record(record)

    async def _validate(self, config: HubConfig) -> None:
        for validator in self._validators:
            await validator(config)

    async def _validate_with_audit(self, config: HubConfig) -> None:
        try:
            await self._validate(config)
        except Exception as error:
            self.last_error = type(error).__name__
            self.audit.append(
                ConfigAudit("validate", False, None, datetime.now(UTC), self.last_error)
            )
            raise

    @staticmethod
    def _version_from_record(record: ConfigVersionRecord) -> DatabaseConfigVersion:
        created_at = record.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        published_at = record.published_at
        if published_at is not None and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        return DatabaseConfigVersion(
            version=record.id,
            status=record.status,
            content_hash=record.content_hash,
            config=HubConfig.model_validate(record.content),
            created_by=record.created_by,
            created_at=created_at,
            published_at=published_at,
            rollback_from_version=record.rollback_from_version,
        )

    @staticmethod
    def _snapshot(version: DatabaseConfigVersion) -> ConfigSnapshot:
        return ConfigSnapshot(
            version=version.version,
            content_hash=version.content_hash,
            config=version.config,
            published_at=version.published_at or version.created_at,
            rollback_from=version.rollback_from_version,
        )
