"""Persistent Skill catalog with execution-time enable checks."""

from __future__ import annotations

import hashlib
from builtins import list as builtin_list
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    ConversationRecord,
    Database,
    DeletionLedgerRecord,
    InteractionTurnRecord,
    MessageRecord,
    SkillDraftRecord,
    SkillRecord,
    SkillRunRecord,
    SkillSuggestionRecord,
    SkillVersionRecord,
)
from app.db.claims import assert_current_claim
from app.ids import uuid7
from app.schemas.evaluation import FixtureEvaluationRequest

from .evaluation import document_hash, evaluate_fixture
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

    @property
    def document(self) -> SkillDocument:
        """当前版本的完整文档（修订生成器的基线）。"""
        return SkillDocument(
            name=self.name,
            description=self.description,
            instructions=self.instructions,
            api=self.api,
        )


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
    target_skill_id: UUID | None = None
    base_version: int | None = None
    verification_report: dict[str, object] | None = None
    verify_status: str | None = None
    verify_reason: str | None = None
    verified_at: datetime | None = None
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
        target_skill_id=item.target_skill_id,
        base_version=item.base_version,
        verification_report=item.verification_report,
        verify_status=item.verify_status,
        verify_reason=item.verify_reason,
        verified_at=(
            item.verified_at.replace(tzinfo=item.verified_at.tzinfo or UTC)
            if item.verified_at
            else None
        ),
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

    async def get_by_name(self, name: str) -> SkillView | None:
        async with self._database.sessions() as session:
            record = await session.scalar(select(SkillRecord).where(SkillRecord.name == name))
            return _view(record) if record is not None else None

    async def get_source_markdown(self, skill_id: UUID) -> str | None:
        async with self._database.sessions() as session:
            record = await session.get(SkillRecord, skill_id)
            if record is None:
                raise LookupError("skill_not_found")
            return record.source_markdown

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

    async def record_audit_suggestion(
        self,
        *,
        skill_id: UUID,
        skill_version: int,
        operation: str,
        reason_code: str,
        kind: str,
        title: str,
        guidance: str,
    ) -> bool:
        """巡检结论落建议流；dedupe 命中或并发撞键返回 False，不重复打扰。"""
        dedupe_key = hashlib.sha256(
            f"audit:{skill_id}:{skill_version}:{operation}:{reason_code}".encode()
        ).hexdigest()
        async with self._database.sessions() as session:
            existing = await session.scalar(
                select(SkillSuggestionRecord.id).where(
                    SkillSuggestionRecord.dedupe_key == dedupe_key
                )
            )
            if existing is not None:
                return False
            session.add(
                SkillSuggestionRecord(
                    id=uuid7(),
                    skill_id=skill_id,
                    skill_version=skill_version,
                    operation=operation,
                    reason_code=reason_code,
                    kind=kind,
                    title=title,
                    guidance=guidance,
                    evidence_run_ids=[],
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
                return False
            return True

    async def suggestions(
        self, *, status: str | None = "pending", limit: int = 50
    ) -> builtin_list[SkillSuggestionView]:
        async with self._database.sessions() as session:
            query = select(SkillSuggestionRecord).order_by(SkillSuggestionRecord.created_at.desc())
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
        target_skill_id: UUID | None = None,
        base_version: int | None = None,
        source_owner_id: UUID | None = None,
        allow_active_source: bool = False,
    ) -> SkillDraftView | None:
        """Persist a proposal for admin review; returns None when already pending.

        target_skill_id 非空时这是对现有技能的修订候选，base_version 记录
        起草基线，审批时校验防止覆盖更新的改动。
        """
        dedupe_key = hashlib.sha256(
            (f"rev:{target_skill_id}:" if target_skill_id else "new:").encode()
            + hashlib.sha256(proposal.document.model_dump_json().encode("utf-8"))
            .hexdigest()
            .encode()
        ).hexdigest()
        moment = datetime.now(UTC)
        async with self._database.sessions() as session:
            if source_owner_id is not None:
                turn = await session.get(InteractionTurnRecord, UUID(turn_id or ""))
                source_states = {"completed"}
                if allow_active_source:
                    source_states |= {"accepted", "thinking", "streaming"}
                if turn is None or turn.state not in source_states:
                    raise ValueError("source_deleted")
                conversation = await session.scalar(
                    select(ConversationRecord)
                    .where(
                        ConversationRecord.id == turn.conversation_id,
                        ConversationRecord.user_id == source_owner_id,
                    )
                    .with_for_update()
                )
                if conversation is None:
                    raise ValueError("source_deleted")
                if allow_active_source:
                    turn = await session.get(
                        InteractionTurnRecord,
                        turn.id,
                        with_for_update=True,
                        populate_existing=True,
                    )
                    if turn is None or turn.state not in source_states:
                        raise ValueError("source_deleted")
                message = await session.get(MessageRecord, turn.input_message_id)
                deleted = await session.scalar(
                    select(DeletionLedgerRecord.id)
                    .where(
                        DeletionLedgerRecord.entity_kind == "message",
                        DeletionLedgerRecord.entity_id == str(turn.conversation_id),
                    )
                    .limit(1)
                )
                if conversation is None or message is None or deleted is not None:
                    raise ValueError("source_deleted")
                # Retry after an interrupted receipt must not create another
                # model-variant proposal for the same source turn and pipeline.
                prior = await session.scalar(
                    select(SkillDraftRecord.id)
                    .where(
                        SkillDraftRecord.turn_id == turn_id,
                        SkillDraftRecord.source == source,
                    )
                    .limit(1)
                )
                if prior is not None:
                    return None

            await assert_current_claim(session)
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
                existing.target_skill_id = target_skill_id
                existing.base_version = base_version
                existing.verification_report = None
                existing.verify_status = None
                existing.verify_reason = None
                existing.verified_at = None
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
                target_skill_id=target_skill_id,
                base_version=base_version,
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

    async def get_draft(self, draft_id: UUID) -> SkillDraftView | None:
        async with self._database.sessions() as session:
            record = await session.get(SkillDraftRecord, draft_id)
            return _draft_view(record) if record is not None else None

    async def pending_revision_exists(self, target_skill_id: UUID) -> bool:
        """同一技能已有待审修订草稿时不重复提案（S4 学习收割去重）。"""
        async with self._database.sessions() as session:
            existing = await session.scalar(
                select(SkillDraftRecord.id)
                .where(
                    SkillDraftRecord.target_skill_id == target_skill_id,
                    SkillDraftRecord.status == "pending",
                )
                .limit(1)
            )
            return existing is not None

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

    async def evaluate_draft(
        self, draft_id: UUID, corpus: FixtureEvaluationRequest
    ) -> SkillDraftView:
        """Pure fixture replay bound to the locked draft and current base version."""
        async with self._database.sessions.begin() as session:
            item = await session.get(SkillDraftRecord, draft_id, with_for_update=True)
            if item is None:
                raise LookupError("skill_draft_not_found")
            if item.status != "pending":
                raise ValueError("skill_draft_already_reviewed")
            baseline = None
            if item.target_skill_id is not None:
                target = await session.get(SkillRecord, item.target_skill_id, with_for_update=True)
                if target is None or target.version != item.base_version:
                    raise ValueError("skill_version_changed")
                baseline = _view(target).document
            report = evaluate_fixture(
                baseline,
                SkillDocument.model_validate(item.document),
                corpus,
                base_version=item.base_version,
            )
            item.verification_report = {
                **(item.verification_report or {}),
                "fixture_replay": report,
            }
            return _draft_view(item)

    async def approve_draft(self, draft_id: UUID) -> SkillView:
        try:
            async with self._database.sessions.begin() as session:
                item = await session.get(SkillDraftRecord, draft_id, with_for_update=True)
                if item is None:
                    raise LookupError("skill_draft_not_found")
                if item.status != "pending":
                    raise ValueError("skill_draft_already_reviewed")
                document = SkillDocument.model_validate(item.document)
                fixture = (item.verification_report or {}).get("fixture_replay")
                if fixture is not None:
                    if fixture.get("candidate_hash") != document_hash(document):
                        raise ValueError("skill_evaluation_stale")
                    if fixture.get("status") == "failed":
                        raise ValueError("skill_fixture_failed")
                if item.target_skill_id is not None:
                    skill = await self._revise_in_session(
                        session,
                        item.target_skill_id,
                        document,
                        base_version=item.base_version,
                    )
                else:
                    skill = await self._create_in_session(session, document, source="generated")
                item.status = "approved"
                item.skill_id = skill.id
                item.reviewed_at = datetime.now(UTC)
                return skill
        except IntegrityError as error:
            raise ValueError("skill_name_exists") from error

    async def revise(
        self,
        skill_id: UUID,
        document: SkillDocument,
        *,
        base_version: int | None = None,
    ) -> SkillView:
        """Apply an approved revision as a new immutable version.

        修订保持技能身份：名称沿用现有技能，其余内容以修订文档为准。
        """
        async with self._database.sessions.begin() as session:
            return await self._revise_in_session(
                session, skill_id, document, base_version=base_version
            )

    async def _revise_in_session(
        self,
        session: AsyncSession,
        skill_id: UUID,
        document: SkillDocument,
        *,
        base_version: int | None,
    ) -> SkillView:
        record = await session.get(SkillRecord, skill_id, with_for_update=True)
        if record is None:
            raise LookupError("skill_not_found")
        if record.enabled:
            raise ValueError("disable_skill_before_edit")
        if base_version is not None and base_version != record.version:
            raise ValueError("skill_version_changed")
        record.description = document.description
        record.instructions = document.instructions
        record.api_manifest = document.api.model_dump(mode="json") if document.api else None
        record.content_hash = hashlib.sha256(
            (
                record.name
                + record.description
                + record.instructions
                + (document.api.model_dump_json() if document.api else "")
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
        return _view(record)

    async def mark_draft_verified(
        self,
        draft_id: UUID,
        *,
        ok: bool,
        reason: str | None = None,
        report: dict[str, object] | None = None,
    ) -> SkillDraftView:
        """Record the outcome of a pre-approval live verification run."""
        async with self._database.sessions() as session:
            item = await session.get(SkillDraftRecord, draft_id, with_for_update=True)
            if item is None:
                raise LookupError("skill_draft_not_found")
            fixture = (item.verification_report or {}).get("fixture_replay")
            item.verification_report = {
                **(report or {}),
                **({"fixture_replay": fixture} if fixture else {}),
            } or None
            item.verify_status = "passed" if ok else "failed"
            item.verify_reason = reason if not ok else None
            item.verified_at = datetime.now(UTC)
            await session.commit()
            return _draft_view(item)

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
        self,
        document: SkillDocument,
        *,
        source: str,
        content_hash: str | None = None,
        source_markdown: str | None = None,
    ) -> SkillView:
        try:
            async with self._database.sessions.begin() as session:
                return await self._create_in_session(
                    session,
                    document,
                    source=source,
                    content_hash=content_hash,
                    source_markdown=source_markdown,
                )
        except IntegrityError as error:
            raise ValueError("skill_name_exists") from error

    async def _create_in_session(
        self,
        session: AsyncSession,
        document: SkillDocument,
        *,
        source: str,
        content_hash: str | None = None,
        source_markdown: str | None = None,
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
            source_markdown=source_markdown,
            content_hash=digest,
            version=1,
            enabled=False,
            created_at=moment,
            updated_at=moment,
        )
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
        await session.flush()
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
