"""Persistent Skill catalog with execution-time enable checks."""

from __future__ import annotations

import hashlib
from builtins import list as builtin_list
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import (
    Database,
    SkillDraftRecord,
    SkillRecord,
    SkillRunRecord,
    SkillSuggestionRecord,
    SkillVersionRecord,
)
from app.ids import uuid7

from .generator import SkillProposal
from .models import SkillApiManifest, SkillDocument


class SkillView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    name: str
    description: str
    instructions: str
    api: SkillApiManifest | None
    source: str
    content_hash: str
    version: int
    enabled: bool
    created_at: datetime
    updated_at: datetime


class SkillVersionView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    description: str
    instructions: str
    api: SkillApiManifest | None
    content_hash: str
    created_at: datetime


class SkillRunView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    skill_id: UUID
    skill_version: int
    connection_id: str
    operation: str
    ok: bool
    reason_code: str | None
    latency_ms: float
    created_at: datetime


class SkillSuggestionView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    skill_id: UUID
    skill_version: int
    operation: str
    reason_code: str
    kind: str
    title: str
    guidance: str
    evidence_run_ids: list[str]
    status: str
    created_at: datetime
    reviewed_at: datetime | None


class SkillDraftView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    system_name: str
    document: SkillDocument
    warnings: list[str]
    evidence: list[str]
    source: str
    turn_id: str | None
    status: str
    skill_id: UUID | None
    created_at: datetime
    reviewed_at: datetime | None


def _draft_view(item: SkillDraftRecord) -> SkillDraftView:
    return SkillDraftView(
        id=item.id,
        system_name=item.system_name,
        document=SkillDocument.model_validate(item.document),
        warnings=item.warnings,
        evidence=item.evidence,
        source=item.source,
        turn_id=item.turn_id,
        status=item.status,
        skill_id=item.skill_id,
        created_at=item.created_at.replace(tzinfo=item.created_at.tzinfo or UTC),
        reviewed_at=(
            item.reviewed_at.replace(tzinfo=item.reviewed_at.tzinfo or UTC)
            if item.reviewed_at
            else None
        ),
    )


def _suggestion_view(item: SkillSuggestionRecord) -> SkillSuggestionView:
    return SkillSuggestionView(
        id=item.id,
        skill_id=item.skill_id,
        skill_version=item.skill_version,
        operation=item.operation,
        reason_code=item.reason_code,
        kind=item.kind,
        title=item.title,
        guidance=item.guidance,
        evidence_run_ids=item.evidence_run_ids,
        status=item.status,
        created_at=item.created_at.replace(tzinfo=item.created_at.tzinfo or UTC),
        reviewed_at=(
            item.reviewed_at.replace(tzinfo=item.reviewed_at.tzinfo or UTC)
            if item.reviewed_at
            else None
        ),
    )


def _failure_advice(reason: str) -> tuple[str, str, str] | None:
    if reason in {
        "connection_http_401",
        "connection_http_403",
        "connection_login_rejected",
    }:
        return ("authentication", "核对连接认证", "检查密钥环境变量、认证方式和服务端授权。")
    if reason == "connection_auth_path_denied":
        return ("connection", "审批登录路径", "核对 Skill 登录路径与连接登录白名单。")
    if reason == "connection_login_invalid_response":
        return ("contract", "核对令牌字段", "对照登录响应检查 Skill 中的 token_field。")
    if reason == "connection_http_404":
        return ("contract", "核对 API 路径", "对照原始 API 文档和服务端版本，检查路径模板。")
    if reason in {"connection_http_400", "connection_http_422"}:
        return ("contract", "核对 API 参数", "对照原始 API 文档，检查参数位置、类型和必填项。")
    if reason == "connection_secret_unavailable":
        return ("connection", "补全连接密钥", "检查服务端密钥环境变量及连接配置。")
    if reason in {"connection_request_failed", "connection_invalid_response"}:
        return ("integration", "检查接口响应", "核对服务可达性、响应格式和 API 契约。")
    return None


def _view(record: SkillRecord) -> SkillView:
    return SkillView(
        id=record.id,
        name=record.name,
        description=record.description,
        instructions=record.instructions,
        api=SkillApiManifest.model_validate(record.api_manifest) if record.api_manifest else None,
        source=record.source,
        content_hash=record.content_hash,
        version=record.version,
        enabled=record.enabled,
        created_at=record.created_at.replace(tzinfo=record.created_at.tzinfo or UTC),
        updated_at=record.updated_at.replace(tzinfo=record.updated_at.tzinfo or UTC),
    )


class SkillStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list(self, *, enabled_only: bool = False) -> list[SkillView]:
        async with self._database.sessions() as session:
            query = select(SkillRecord).order_by(SkillRecord.name)
            if enabled_only:
                query = query.where(SkillRecord.enabled.is_(True))
            return [_view(item) for item in (await session.scalars(query)).all()]

    async def get(self, skill_id: UUID) -> SkillView | None:
        async with self._database.sessions() as session:
            record = await session.get(SkillRecord, skill_id)
            return _view(record) if record is not None else None

    async def record_run(
        self,
        *,
        skill_id: UUID,
        skill_version: int,
        connection_id: str,
        operation: str,
        ok: bool,
        reason_code: str | None,
        latency_ms: float,
    ) -> None:
        async with self._database.sessions() as session:
            session.add(
                SkillRunRecord(
                    id=uuid7(),
                    skill_id=skill_id,
                    skill_version=skill_version,
                    connection_id=connection_id,
                    operation=operation,
                    ok=ok,
                    reason_code=reason_code,
                    latency_ms=latency_ms,
                    created_at=datetime.now(UTC),
                )
            )
            await session.commit()
        if not ok and reason_code and _failure_advice(reason_code) is not None:
            await self._maybe_suggest(skill_id, skill_version, operation, reason_code)

    async def _maybe_suggest(
        self, skill_id: UUID, version: int, operation: str, reason: str
    ) -> None:
        advice = _failure_advice(reason)
        if advice is None:
            return
        dedupe_key = hashlib.sha256(
            f"{skill_id}:{version}:{operation}:{reason}".encode()
        ).hexdigest()
        async with self._database.sessions() as session:
            existing = await session.scalar(
                select(SkillSuggestionRecord.id).where(
                    SkillSuggestionRecord.dedupe_key == dedupe_key
                )
            )
            if existing is not None:
                return
            recent = (
                await session.scalars(
                    select(SkillRunRecord)
                    .where(
                        SkillRunRecord.skill_id == skill_id,
                        SkillRunRecord.skill_version == version,
                        SkillRunRecord.operation == operation,
                    )
                    .order_by(SkillRunRecord.created_at.desc())
                    .limit(10)
                )
            ).all()
            matching = [item for item in recent if item.reason_code == reason]
            if len(matching) < 3:
                return
            kind, title, guidance = advice
            session.add(
                SkillSuggestionRecord(
                    id=uuid7(),
                    skill_id=skill_id,
                    skill_version=version,
                    operation=operation,
                    reason_code=reason,
                    kind=kind,
                    title=title,
                    guidance=guidance,
                    evidence_run_ids=[str(item.id) for item in matching[:10]],
                    dedupe_key=dedupe_key,
                    status="pending",
                    created_at=datetime.now(UTC),
                    reviewed_at=None,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()

    async def suggestions(
        self, *, status: str | None = "pending", limit: int = 50
    ) -> builtin_list[SkillSuggestionView]:
        async with self._database.sessions() as session:
            query = select(SkillSuggestionRecord).order_by(
                SkillSuggestionRecord.created_at.desc()
            )
            if status is not None:
                query = query.where(SkillSuggestionRecord.status == status)
            query = query.limit(min(max(limit, 1), 100))
            return [_suggestion_view(item) for item in (await session.scalars(query)).all()]

    async def dismiss_suggestion(self, suggestion_id: UUID) -> SkillSuggestionView:
        async with self._database.sessions() as session:
            item = await session.get(SkillSuggestionRecord, suggestion_id, with_for_update=True)
            if item is None:
                raise LookupError("skill_suggestion_not_found")
            item.status = "dismissed"
            item.reviewed_at = datetime.now(UTC)
            await session.commit()
            return _suggestion_view(item)

    async def save_draft(
        self,
        proposal: SkillProposal,
        *,
        system_name: str,
        source: str,
        turn_id: str | None = None,
    ) -> SkillDraftView | None:
        """Persist a proposal for admin review; returns None when already pending."""
        dedupe_key = hashlib.sha256(
            (
                system_name
                + ":"
                + hashlib.sha256(
                    proposal.document.model_dump_json().encode("utf-8")
                ).hexdigest()
            ).encode()
        ).hexdigest()
        moment = datetime.now(UTC)
        async with self._database.sessions() as session:
            existing = await session.scalar(
                select(SkillDraftRecord)
                .where(SkillDraftRecord.dedupe_key == dedupe_key)
                .with_for_update()
            )
            if existing is not None:
                if existing.status == "pending":
                    return None
                existing.status = "pending"
                existing.source = source
                existing.turn_id = turn_id
                existing.document = proposal.document.model_dump(mode="json")
                existing.warnings = proposal.warnings
                existing.evidence = proposal.evidence
                existing.skill_id = None
                existing.created_at = moment
                existing.reviewed_at = None
                await session.commit()
                return _draft_view(existing)
            record = SkillDraftRecord(
                id=uuid7(),
                system_name=system_name,
                document=proposal.document.model_dump(mode="json"),
                warnings=proposal.warnings,
                evidence=proposal.evidence,
                source=source,
                turn_id=turn_id,
                dedupe_key=dedupe_key,
                status="pending",
                skill_id=None,
                created_at=moment,
                reviewed_at=None,
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            return _draft_view(record)

    async def drafts(
        self, *, status: str | None = "pending", limit: int = 50
    ) -> builtin_list[SkillDraftView]:
        async with self._database.sessions() as session:
            query = select(SkillDraftRecord).order_by(SkillDraftRecord.created_at.desc())
            if status is not None:
                query = query.where(SkillDraftRecord.status == status)
            query = query.limit(min(max(limit, 1), 100))
            return [_draft_view(item) for item in (await session.scalars(query)).all()]

    async def dismiss_draft(self, draft_id: UUID) -> SkillDraftView:
        async with self._database.sessions() as session:
            item = await session.get(SkillDraftRecord, draft_id, with_for_update=True)
            if item is None:
                raise LookupError("skill_draft_not_found")
            if item.status != "pending":
                raise ValueError("skill_draft_already_reviewed")
            item.status = "dismissed"
            item.reviewed_at = datetime.now(UTC)
            await session.commit()
            return _draft_view(item)

    async def approve_draft(self, draft_id: UUID) -> SkillView:
        async with self._database.sessions() as session:
            item = await session.get(SkillDraftRecord, draft_id, with_for_update=True)
            if item is None:
                raise LookupError("skill_draft_not_found")
            if item.status != "pending":
                raise ValueError("skill_draft_already_reviewed")
            document = SkillDocument.model_validate(item.document)
        skill = await self.create(document, source="generated")
        async with self._database.sessions() as session:
            item = await session.get(SkillDraftRecord, draft_id, with_for_update=True)
            if item is not None and item.status == "pending":
                item.status = "approved"
                item.skill_id = skill.id
                item.reviewed_at = datetime.now(UTC)
                await session.commit()
        return skill

    async def runs(self, skill_id: UUID, *, limit: int = 50) -> builtin_list[SkillRunView]:
        if await self.get(skill_id) is None:
            raise LookupError("skill_not_found")
        async with self._database.sessions() as session:
            query = (
                select(SkillRunRecord)
                .where(SkillRunRecord.skill_id == skill_id)
                .order_by(SkillRunRecord.created_at.desc())
                .limit(min(max(limit, 1), 100))
            )
            return [
                SkillRunView(
                    id=item.id,
                    skill_id=item.skill_id,
                    skill_version=item.skill_version,
                    connection_id=item.connection_id,
                    operation=item.operation,
                    ok=item.ok,
                    reason_code=item.reason_code,
                    latency_ms=item.latency_ms,
                    created_at=item.created_at.replace(tzinfo=item.created_at.tzinfo or UTC),
                )
                for item in (await session.scalars(query)).all()
            ]

    async def create(
        self, document: SkillDocument, *, source: str, content_hash: str | None = None
    ) -> SkillView:
        moment = datetime.now(UTC)
        digest = (
            content_hash or hashlib.sha256(document.model_dump_json().encode("utf-8")).hexdigest()
        )
        record = SkillRecord(
            id=uuid7(),
            name=document.name,
            description=document.description,
            instructions=document.instructions,
            api_manifest=document.api.model_dump(mode="json") if document.api else None,
            source=source,
            content_hash=digest,
            version=1,
            enabled=False,
            created_at=moment,
            updated_at=moment,
        )
        async with self._database.sessions() as session:
            session.add(record)
            session.add(
                SkillVersionRecord(
                    id=uuid7(),
                    skill_id=record.id,
                    version=1,
                    description=record.description,
                    instructions=record.instructions,
                    api_manifest=record.api_manifest,
                    content_hash=digest,
                    created_at=moment,
                )
            )
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise ValueError("skill_name_exists") from error
        return _view(record)

    async def set_enabled(self, skill_id: UUID, enabled: bool) -> SkillView:
        async with self._database.sessions() as session:
            record = await session.get(SkillRecord, skill_id)
            if record is None:
                raise LookupError("skill_not_found")
            record.enabled = enabled
            record.updated_at = datetime.now(UTC)
            await session.commit()
            return _view(record)

    async def set_api(self, skill_id: UUID, api: SkillApiManifest) -> SkillView:
        """Complete the API contract of a disabled imported Skill."""
        async with self._database.sessions() as session:
            record = await session.get(SkillRecord, skill_id, with_for_update=True)
            if record is None:
                raise LookupError("skill_not_found")
            if record.enabled:
                raise ValueError("disable_skill_before_edit")
            record.api_manifest = api.model_dump(mode="json")
            record.content_hash = hashlib.sha256(
                (
                    record.name + record.description + record.instructions + api.model_dump_json()
                ).encode("utf-8")
            ).hexdigest()
            moment = datetime.now(UTC)
            record.version += 1
            record.updated_at = moment
            session.add(
                SkillVersionRecord(
                    id=uuid7(),
                    skill_id=record.id,
                    version=record.version,
                    description=record.description,
                    instructions=record.instructions,
                    api_manifest=record.api_manifest,
                    content_hash=record.content_hash,
                    created_at=moment,
                )
            )
            await session.commit()
            return _view(record)

    async def versions(self, skill_id: UUID) -> builtin_list[SkillVersionView]:
        if await self.get(skill_id) is None:
            raise LookupError("skill_not_found")
        async with self._database.sessions() as session:
            query = (
                select(SkillVersionRecord)
                .where(SkillVersionRecord.skill_id == skill_id)
                .order_by(SkillVersionRecord.version.desc())
            )
            return [
                SkillVersionView(
                    version=item.version,
                    description=item.description,
                    instructions=item.instructions,
                    api=(
                        SkillApiManifest.model_validate(item.api_manifest)
                        if item.api_manifest
                        else None
                    ),
                    content_hash=item.content_hash,
                    created_at=item.created_at.replace(tzinfo=item.created_at.tzinfo or UTC),
                )
                for item in (await session.scalars(query)).all()
            ]

    async def rollback(self, skill_id: UUID, version: int) -> SkillView:
        async with self._database.sessions() as session:
            record = await session.get(SkillRecord, skill_id, with_for_update=True)
            if record is None:
                raise LookupError("skill_not_found")
            if record.enabled:
                raise ValueError("disable_skill_before_rollback")
            query = select(SkillVersionRecord).where(
                SkillVersionRecord.skill_id == skill_id,
                SkillVersionRecord.version == version,
            )
            previous = (await session.scalars(query)).one_or_none()
            if previous is None:
                raise LookupError("skill_version_not_found")
            moment = datetime.now(UTC)
            record.version += 1
            record.description = previous.description
            record.instructions = previous.instructions
            record.api_manifest = previous.api_manifest
            record.content_hash = previous.content_hash
            record.updated_at = moment
            session.add(
                SkillVersionRecord(
                    id=uuid7(),
                    skill_id=skill_id,
                    version=record.version,
                    description=previous.description,
                    instructions=previous.instructions,
                    api_manifest=previous.api_manifest,
                    content_hash=previous.content_hash,
                    created_at=moment,
                )
            )
            await session.commit()
            return _view(record)
