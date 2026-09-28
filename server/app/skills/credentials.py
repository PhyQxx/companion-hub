"""Encrypted, admin-managed credentials scoped to one installed Skill."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict

from app.db import Database, SkillCredentialRecord, SkillRecord

from .models import SkillApiManifest


class SkillCredentialError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class SkillCredentialStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_id: UUID
    connection_id: str | None
    configured: bool
    key_ready: bool
    updated_at: datetime | None


class SkillCredentialStore:
    def __init__(self, database: Database, *, key: str | bytes | None = None) -> None:
        self._database = database
        value = key if key is not None else os.getenv("ARIA_SKILL_CREDENTIAL_KEY")
        try:
            self._cipher = Fernet(value) if value else None
        except (TypeError, ValueError):
            self._cipher = None

    @property
    def key_ready(self) -> bool:
        return self._cipher is not None

    async def status(self, skill_id: UUID) -> SkillCredentialStatus:
        async with self._database.sessions() as session:
            skill = await session.get(SkillRecord, skill_id)
            if skill is None:
                raise LookupError("skill_not_found")
            record = await session.get(SkillCredentialRecord, skill_id)
            connection_id = (
                skill.api_manifest.get("connection") if skill.api_manifest is not None else None
            )
            key_ready = self.key_ready
            if record is not None and self._cipher is not None:
                try:
                    self._cipher.decrypt(record.ciphertext.encode())
                except InvalidToken:
                    key_ready = False
            return SkillCredentialStatus(
                skill_id=skill_id,
                connection_id=connection_id,
                configured=bool(record is not None and record.connection_id == connection_id),
                key_ready=key_ready,
                updated_at=(
                    record.updated_at.replace(tzinfo=record.updated_at.tzinfo or UTC)
                    if record is not None
                    else None
                ),
            )

    async def put(self, skill_id: UUID, username: str, password: str) -> SkillCredentialStatus:
        if self._cipher is None:
            raise SkillCredentialError("skill_credential_key_unavailable")
        if not username or not password:
            raise SkillCredentialError("skill_credential_empty")
        async with self._database.sessions() as session:
            skill = await session.get(SkillRecord, skill_id, with_for_update=True)
            if skill is None:
                raise LookupError("skill_not_found")
            api = (
                SkillApiManifest.model_validate(skill.api_manifest)
                if skill.api_manifest is not None
                else None
            )
            if api is None or api.auth is None or api.auth.type != "login_bearer":
                raise SkillCredentialError("skill_login_auth_required")
            payload = json.dumps(
                {
                    "skill_id": str(skill_id),
                    "connection_id": api.connection,
                    "username": username,
                    "password": password,
                },
                ensure_ascii=False,
            ).encode()
            record = await session.get(SkillCredentialRecord, skill_id, with_for_update=True)
            if record is None:
                record = SkillCredentialRecord(skill_id=skill_id)
                session.add(record)
            record.connection_id = api.connection
            record.ciphertext = self._cipher.encrypt(payload).decode()
            record.updated_at = datetime.now(UTC)
            await session.commit()
        return await self.status(skill_id)

    async def get(self, skill_id: UUID, connection_id: str) -> tuple[str, str, str] | None:
        async with self._database.sessions() as session:
            record = await session.get(SkillCredentialRecord, skill_id)
            if record is None or record.connection_id != connection_id:
                return None
            if self._cipher is None:
                raise SkillCredentialError("skill_credential_key_unavailable")
            try:
                payload = json.loads(self._cipher.decrypt(record.ciphertext.encode()))
            except (InvalidToken, ValueError) as error:
                raise SkillCredentialError("skill_credential_decryption_failed") from error
            if (
                not isinstance(payload, dict)
                or payload.get("skill_id") != str(skill_id)
                or payload.get("connection_id") != connection_id
                or not isinstance(payload.get("username"), str)
                or not isinstance(payload.get("password"), str)
            ):
                raise SkillCredentialError("skill_credential_decryption_failed")
            version = record.updated_at.isoformat()
            return payload["username"], payload["password"], version

    async def delete(self, skill_id: UUID) -> SkillCredentialStatus:
        async with self._database.sessions() as session:
            skill = await session.get(SkillRecord, skill_id)
            if skill is None:
                raise LookupError("skill_not_found")
            record = await session.get(SkillCredentialRecord, skill_id, with_for_update=True)
            if record is not None:
                await session.delete(record)
                await session.commit()
        return await self.status(skill_id)
