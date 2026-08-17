from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.db import Database, PersonaPointerRecord, PersonaVersionRecord

from .models import PersonaConfig


def hash_persona(persona: PersonaConfig) -> str:
    payload = json.dumps(
        persona.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PersonaVersion:
    version: int
    status: str
    content_hash: str
    persona: PersonaConfig
    created_by: str
    created_at: datetime
    published_at: datetime | None
    rollback_from_version: int | None


@dataclass(frozen=True, slots=True)
class PersonaSnapshot:
    version: int
    content_hash: str
    persona: PersonaConfig
    published_at: datetime
    rollback_from: int | None = None


class PersonaStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = asyncio.Lock()
        self._current: PersonaSnapshot | None = None

    @property
    def current(self) -> PersonaSnapshot:
        if self._current is None:
            raise RuntimeError("persona has not been loaded")
        return self._current

    async def load(self) -> PersonaSnapshot:
        async with self._lock:
            version = await self._read_current()
            if version is None:
                persona = PersonaConfig()
                now = datetime.now(UTC)
                async with self._database.sessions.begin() as session:
                    record = PersonaVersionRecord(
                        status="published",
                        content=persona.model_dump(mode="json"),
                        content_hash=hash_persona(persona),
                        created_by="bootstrap",
                        published_at=now,
                    )
                    session.add(record)
                    await session.flush()
                    session.add(PersonaPointerRecord(id=1, current_version_id=record.id))
                version = self._from_record(record)
            self._current = self._snapshot(version)
            return self._current

    async def create_draft(self, persona: PersonaConfig, *, actor: str) -> PersonaVersion:
        async with self._database.sessions.begin() as session:
            record = PersonaVersionRecord(
                status="draft",
                content=persona.model_dump(mode="json"),
                content_hash=hash_persona(persona),
                created_by=actor,
            )
            session.add(record)
            await session.flush()
            await session.refresh(record)
        return self._from_record(record)

    async def publish(self, version: int) -> PersonaSnapshot:
        async with self._lock:
            now = datetime.now(UTC)
            async with self._database.sessions.begin() as session:
                pointer = await session.scalar(
                    select(PersonaPointerRecord)
                    .where(PersonaPointerRecord.id == 1)
                    .with_for_update()
                )
                target = await session.get(PersonaVersionRecord, version)
                if pointer is None:
                    raise RuntimeError("persona pointer is missing")
                if target is None:
                    raise LookupError(f"persona version not found: {version}")
                if target.status != "draft":
                    raise ValueError("only a draft persona can be published")
                current = await session.get(PersonaVersionRecord, pointer.current_version_id)
                if current is None:
                    raise RuntimeError("published persona is missing")
                current.status = "superseded"
                target.status = "published"
                target.published_at = now
                pointer.current_version_id = target.id
                await session.refresh(target)
            result = self._from_record(target)
            self._current = self._snapshot(result)
            return self._current

    async def rollback(self, version: int, *, actor: str) -> PersonaSnapshot:
        async with self._lock:
            target = await self.get_version(version)
            if target.status == "draft":
                raise ValueError("a draft cannot be used as a rollback target")
            now = datetime.now(UTC)
            async with self._database.sessions.begin() as session:
                pointer = await session.scalar(
                    select(PersonaPointerRecord)
                    .where(PersonaPointerRecord.id == 1)
                    .with_for_update()
                )
                if pointer is None:
                    raise RuntimeError("persona pointer is missing")
                current = await session.get(PersonaVersionRecord, pointer.current_version_id)
                if current is None:
                    raise RuntimeError("published persona is missing")
                current.status = "superseded"
                restored = PersonaVersionRecord(
                    status="published",
                    content=target.persona.model_dump(mode="json"),
                    content_hash=target.content_hash,
                    created_by=actor,
                    published_at=now,
                    rollback_from_version=target.version,
                )
                session.add(restored)
                await session.flush()
                pointer.current_version_id = restored.id
                await session.refresh(restored)
            result = self._from_record(restored)
            self._current = self._snapshot(result)
            return self._current

    async def list_versions(self, *, limit: int = 50) -> list[PersonaVersion]:
        async with self._database.sessions() as session:
            records = await session.scalars(
                select(PersonaVersionRecord).order_by(PersonaVersionRecord.id.desc()).limit(limit)
            )
            return [self._from_record(record) for record in records]

    async def get_version(self, version: int) -> PersonaVersion:
        async with self._database.sessions() as session:
            record = await session.get(PersonaVersionRecord, version)
        if record is None:
            raise LookupError(f"persona version not found: {version}")
        return self._from_record(record)

    async def _read_current(self) -> PersonaVersion | None:
        async with self._database.sessions() as session:
            pointer = await session.get(PersonaPointerRecord, 1)
            if pointer is None:
                return None
            record = await session.get(PersonaVersionRecord, pointer.current_version_id)
        if record is None:
            raise RuntimeError("published persona is missing")
        return self._from_record(record)

    @staticmethod
    def _from_record(record: PersonaVersionRecord) -> PersonaVersion:
        created_at = record.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        published_at = record.published_at
        if published_at is not None and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        return PersonaVersion(
            version=record.id,
            status=record.status,
            content_hash=record.content_hash,
            persona=PersonaConfig.model_validate(record.content),
            created_by=record.created_by,
            created_at=created_at,
            published_at=published_at,
            rollback_from_version=record.rollback_from_version,
        )

    @staticmethod
    def _snapshot(version: PersonaVersion) -> PersonaSnapshot:
        return PersonaSnapshot(
            version=version.version,
            content_hash=version.content_hash,
            persona=version.persona,
            published_at=version.published_at or version.created_at,
            rollback_from=version.rollback_from_version,
        )
