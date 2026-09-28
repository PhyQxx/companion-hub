"""Admin Skill catalog and ZIP import."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import Field, SecretStr
from sqlalchemy.exc import DBAPIError

from app.schemas import PrivacyLevel
from app.schemas.common import StrictModel
from app.skills import SkillApiManifest, SkillDocument, import_skill_zip
from app.skills.connections import (
    SkillConnection,
    SkillConnectionStore,
    SkillConnectionView,
    SkillHttpClient,
)
from app.skills.credentials import (
    SkillCredentialError,
    SkillCredentialStatus,
    SkillCredentialStore,
)
from app.skills.drafts import verify_skill_draft
from app.skills.generator import SkillDraftGenerator, SkillProposal, extract_document
from app.skills.runtime import SkillToolProvider
from app.skills.store import (
    SkillDraftView,
    SkillRunView,
    SkillStore,
    SkillSuggestionView,
    SkillVersionView,
    SkillView,
)

from .admin_config import AdminTokenGuard


class SkillEnabledRequest(StrictModel):
    enabled: bool


class SkillRollbackRequest(StrictModel):
    version: int


class SkillPreviewRequest(StrictModel):
    text: str
    privacy_level: PrivacyLevel = PrivacyLevel.L1


class SkillPreviewResponse(StrictModel):
    tools: list[str]
    guidance: str


class SkillGenerateRequest(StrictModel):
    system_name: str
    source: str


class SkillCredentialRequest(StrictModel):
    username: SecretStr = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1, max_length=4096)


def _skill_storage_unavailable(error: DBAPIError) -> HTTPException | None:
    original = error.orig
    if getattr(original, "sqlstate", None) == "42P01" or "no such table" in str(original):
        return HTTPException(status_code=503, detail="skill_schema_not_migrated")
    return None


def create_admin_skills_router(
    store: SkillStore,
    *,
    admin_token: str | None,
    tool_provider: SkillToolProvider | None = None,
    generator: SkillDraftGenerator | None = None,
    connections: SkillConnectionStore | None = None,
    credentials: SkillCredentialStore | None = None,
    http_client: SkillHttpClient | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/skills",
        tags=["admin-skills"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("", response_model=list[SkillView])
    async def list_skills() -> list[SkillView]:
        try:
            return await store.list()
        except DBAPIError as error:
            failure = _skill_storage_unavailable(error)
            if failure is not None:
                raise failure from error
            raise

    @router.post("", response_model=SkillView, status_code=201)
    async def create_skill(body: SkillDocument) -> SkillView:
        try:
            return await store.create(body, source="created")
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except DBAPIError as error:
            failure = _skill_storage_unavailable(error)
            if failure is not None:
                raise failure from error
            raise

    @router.post("/generated", response_model=SkillView, status_code=201)
    async def save_generated_skill(body: SkillDocument) -> SkillView:
        try:
            return await store.create(body, source="generated")
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except DBAPIError as error:
            failure = _skill_storage_unavailable(error)
            if failure is not None:
                raise failure from error
            raise

    @router.post("/import", response_model=SkillView, status_code=201)
    async def import_skill(file: Annotated[UploadFile, File()]) -> SkillView:
        data = await file.read(2 * 1024 * 1024 + 1)
        try:
            document, digest = import_skill_zip(data)
            return await store.create(document, source="uploaded", content_hash=digest)
        except (UnicodeError, ValueError) as error:
            code = 409 if str(error) == "skill_name_exists" else 422
            raise HTTPException(status_code=code, detail=str(error)) from error
        except DBAPIError as error:
            failure = _skill_storage_unavailable(error)
            if failure is not None:
                raise failure from error
            raise

    @router.post("/preview", response_model=SkillPreviewResponse)
    async def preview(body: SkillPreviewRequest) -> SkillPreviewResponse:
        if tool_provider is None:
            return SkillPreviewResponse(tools=[], guidance="")
        handlers = await tool_provider.select(body.text, privacy_level=body.privacy_level)
        return SkillPreviewResponse(
            tools=[handler.name for handler in handlers],
            guidance=await tool_provider.guidance(body.text),
        )

    @router.post("/generate", response_model=SkillProposal)
    async def generate_skill(body: SkillGenerateRequest) -> SkillProposal:
        if generator is None:
            raise HTTPException(status_code=503, detail="skill_generator_unconfigured")
        try:
            return await generator.generate(body.source, system_name=body.system_name)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.post("/generate-upload", response_model=SkillProposal)
    async def generate_skill_upload(
        file: Annotated[UploadFile, File()], system_name: str
    ) -> SkillProposal:
        if generator is None:
            raise HTTPException(status_code=503, detail="skill_generator_unconfigured")
        try:
            source = extract_document(file.filename or "", await file.read(256 * 1024 + 1))
            return await generator.generate(source, system_name=system_name, filename=file.filename)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/connections", response_model=list[SkillConnectionView])
    async def list_connections() -> list[SkillConnectionView]:
        return await connections.list() if connections is not None else []

    @router.put("/connections/{connection_id}", response_model=SkillConnectionView)
    async def put_connection(connection_id: str, body: SkillConnection) -> SkillConnectionView:
        if connections is None:
            raise HTTPException(status_code=503, detail="skill_connections_unconfigured")
        if connection_id != body.id:
            raise HTTPException(status_code=422, detail="connection_id_mismatch")
        return await connections.put(body)

    @router.get("/{skill_id}/credential", response_model=SkillCredentialStatus)
    async def credential_status(skill_id: UUID) -> SkillCredentialStatus:
        if credentials is None:
            raise HTTPException(status_code=503, detail="skill_credentials_unconfigured")
        try:
            return await credentials.status(skill_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.put("/{skill_id}/credential", response_model=SkillCredentialStatus)
    async def save_credential(
        skill_id: UUID, body: SkillCredentialRequest
    ) -> SkillCredentialStatus:
        if credentials is None:
            raise HTTPException(status_code=503, detail="skill_credentials_unconfigured")
        try:
            return await credentials.put(
                skill_id,
                body.username.get_secret_value(),
                body.password.get_secret_value(),
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except SkillCredentialError as error:
            status_code = 503 if error.reason_code == "skill_credential_key_unavailable" else 409
            raise HTTPException(status_code=status_code, detail=error.reason_code) from error

    @router.delete("/{skill_id}/credential", response_model=SkillCredentialStatus)
    async def delete_credential(skill_id: UUID) -> SkillCredentialStatus:
        if credentials is None:
            raise HTTPException(status_code=503, detail="skill_credentials_unconfigured")
        try:
            return await credentials.delete(skill_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.get("/suggestions", response_model=list[SkillSuggestionView])
    async def list_suggestions() -> list[SkillSuggestionView]:
        return await store.suggestions()

    @router.post("/suggestions/{suggestion_id}/dismiss", response_model=SkillSuggestionView)
    async def dismiss_suggestion(suggestion_id: UUID) -> SkillSuggestionView:
        try:
            return await store.dismiss_suggestion(suggestion_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.get("/drafts", response_model=list[SkillDraftView])
    async def list_drafts() -> list[SkillDraftView]:
        try:
            return await store.drafts()
        except DBAPIError as error:
            failure = _skill_storage_unavailable(error)
            if failure is not None:
                raise failure from error
            raise

    @router.post("/drafts/{draft_id}/verify", response_model=SkillDraftView)
    async def verify_draft(draft_id: UUID) -> SkillDraftView:
        if connections is None or http_client is None:
            raise HTTPException(status_code=503, detail="skill_verification_unconfigured")
        draft = await store.get_draft(draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="skill_draft_not_found")
        if draft.status != "pending":
            raise HTTPException(status_code=409, detail="skill_draft_already_reviewed")
        return await verify_skill_draft(
            draft, store=store, connections=connections, http_client=http_client
        )

    @router.post("/drafts/{draft_id}/approve", response_model=SkillView)
    async def approve_draft(draft_id: UUID) -> SkillView:
        try:
            return await store.approve_draft(draft_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/drafts/{draft_id}/dismiss", response_model=SkillDraftView)
    async def dismiss_draft(draft_id: UUID) -> SkillDraftView:
        try:
            return await store.dismiss_draft(draft_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.get("/{skill_id}", response_model=SkillView)
    async def get_skill(skill_id: UUID) -> SkillView:
        item = await store.get(skill_id)
        if item is None:
            raise HTTPException(status_code=404, detail="skill_not_found")
        return item

    @router.put("/{skill_id}/enabled", response_model=SkillView)
    async def set_enabled(skill_id: UUID, body: SkillEnabledRequest) -> SkillView:
        try:
            return await store.set_enabled(skill_id, body.enabled)
        except LookupError as error:
            raise HTTPException(status_code=404, detail="skill_not_found") from error

    @router.put("/{skill_id}/api", response_model=SkillView)
    async def set_api(skill_id: UUID, body: SkillApiManifest) -> SkillView:
        try:
            return await store.set_api(skill_id, body)
        except LookupError as error:
            raise HTTPException(status_code=404, detail="skill_not_found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.get("/{skill_id}/versions", response_model=list[SkillVersionView])
    async def list_versions(skill_id: UUID) -> list[SkillVersionView]:
        try:
            return await store.versions(skill_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.get("/{skill_id}/runs", response_model=list[SkillRunView])
    async def list_runs(skill_id: UUID) -> list[SkillRunView]:
        try:
            return await store.runs(skill_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.post("/{skill_id}/rollback", response_model=SkillView)
    async def rollback(skill_id: UUID, body: SkillRollbackRequest) -> SkillView:
        try:
            return await store.rollback(skill_id, body.version)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return router
