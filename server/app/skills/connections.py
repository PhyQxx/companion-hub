"""Admin-configured, allowlisted HTTP connections for declarative read Skills."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.db import Database, SkillConnectionRecord
from app.llm import EnvSecretProvider, SecretNotFound

from .credentials import SkillCredentialError, SkillCredentialStore
from .models import SkillLoginAuth

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PATH = re.compile(r"^/(?!/)[A-Za-z0-9_/{}/.-]+$")
_SECRET_REF = re.compile(r"^env:[A-Z][A-Z0-9_]{2,127}$")
_HEADER = re.compile(r"^X-[A-Za-z0-9-]{1,78}$", re.IGNORECASE)


class SkillConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=1, max_length=500)
    auth_type: str = Field(pattern=r"^(none|bearer|header|login_bearer)$")
    secret_ref: str | None = None
    username_ref: str | None = None
    header_name: str | None = None
    allowed_paths: list[str] = Field(min_length=1, max_length=50)
    allowed_write_paths: list[str] = Field(default_factory=list, max_length=50)
    allowed_auth_paths: list[str] = Field(default_factory=list, max_length=10)
    enabled: bool = False

    @model_validator(mode="after")
    def validate_config(self) -> SkillConnection:
        if not _SLUG.fullmatch(self.id) or self.id == "pnkx":
            raise ValueError("invalid_or_reserved_connection_id")
        url = urlsplit(self.base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or (
                url.path not in {"", "/"}
                and (
                    not _PATH.fullmatch(url.path)
                    or "{" in url.path
                    or "}" in url.path
                    or any(part in {".", ".."} for part in url.path.split("/"))
                )
            )
        ):
            raise ValueError("connection_requires_https_base_url")
        if self.auth_type == "none":
            if self.secret_ref is not None or self.header_name is not None:
                raise ValueError("auth_fields_not_allowed")
        elif self.auth_type != "login_bearer" and (
            self.secret_ref is None or not _SECRET_REF.fullmatch(self.secret_ref)
        ):
            raise ValueError("connection_requires_env_secret_ref")
        if self.auth_type == "login_bearer":
            if (self.username_ref is None) != (self.secret_ref is None):
                raise ValueError("connection_login_refs_must_be_paired")
            if self.username_ref is not None and not _SECRET_REF.fullmatch(self.username_ref):
                raise ValueError("connection_requires_username_ref")
            if self.secret_ref is not None and not _SECRET_REF.fullmatch(self.secret_ref):
                raise ValueError("connection_requires_env_secret_ref")
            if not self.allowed_auth_paths:
                raise ValueError("connection_requires_auth_path")
        elif self.username_ref is not None or self.allowed_auth_paths:
            raise ValueError("login_fields_not_allowed")
        if self.auth_type == "header":
            if not self.header_name or not _HEADER.fullmatch(self.header_name):
                raise ValueError("invalid_auth_header_name")
        elif self.header_name is not None:
            raise ValueError("header_name_not_allowed")
        if (
            len(set(self.allowed_paths)) != len(self.allowed_paths)
            or len(set(self.allowed_auth_paths)) != len(self.allowed_auth_paths)
            or len(set(self.allowed_write_paths)) != len(self.allowed_write_paths)
        ):
            raise ValueError("duplicate_allowed_path")
        for path in [*self.allowed_paths, *self.allowed_auth_paths, *self.allowed_write_paths]:
            if (
                not _PATH.fullmatch(path)
                or any(part in {".", ".."} for part in path.split("/"))
                or "{" in re.sub(r"\{[a-z][A-Za-z0-9_]*\}", "", path)
                or "}" in re.sub(r"\{[a-z][A-Za-z0-9_]*\}", "", path)
            ):
                raise ValueError("invalid_allowed_path")
        if any("{" in path or "}" in path for path in self.allowed_auth_paths):
            raise ValueError("invalid_auth_path")
        return self


class SkillConnectionView(SkillConnection):
    updated_at: datetime


def _view(record: SkillConnectionRecord) -> SkillConnectionView:
    return SkillConnectionView(
        id=record.id,
        base_url=record.base_url,
        auth_type=record.auth_type,
        secret_ref=record.secret_ref,
        username_ref=record.username_ref,
        header_name=record.header_name,
        allowed_paths=record.allowed_paths,
        allowed_write_paths=record.allowed_write_paths,
        allowed_auth_paths=record.allowed_auth_paths,
        enabled=record.enabled,
        updated_at=record.updated_at.replace(tzinfo=record.updated_at.tzinfo or UTC),
    )


class SkillConnectionStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list(self) -> list[SkillConnectionView]:
        async with self._database.sessions() as session:
            query = select(SkillConnectionRecord).order_by(SkillConnectionRecord.id)
            return [_view(item) for item in (await session.scalars(query)).all()]

    async def get(self, connection_id: str) -> SkillConnectionView | None:
        async with self._database.sessions() as session:
            record = await session.get(SkillConnectionRecord, connection_id)
            return _view(record) if record else None

    async def put(self, config: SkillConnection) -> SkillConnectionView:
        async with self._database.sessions() as session:
            record = await session.get(SkillConnectionRecord, config.id, with_for_update=True)
            if record is None:
                record = SkillConnectionRecord(id=config.id)
                session.add(record)
            record.base_url = config.base_url.rstrip("/")
            record.auth_type = config.auth_type
            record.secret_ref = config.secret_ref
            record.username_ref = config.username_ref
            record.header_name = config.header_name
            record.allowed_paths = list(config.allowed_paths)
            record.allowed_write_paths = list(config.allowed_write_paths)
            record.allowed_auth_paths = list(config.allowed_auth_paths)
            record.enabled = config.enabled
            record.updated_at = datetime.now(UTC)
            await session.commit()
            return _view(record)


class SkillConnectionError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class SkillHttpClient:
    def __init__(
        self,
        store: SkillConnectionStore,
        *,
        secrets: EnvSecretProvider | None = None,
        credentials: SkillCredentialStore | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = store
        self._secrets = secrets or EnvSecretProvider()
        self._credentials = credentials
        self._client = client or httpx.AsyncClient(
            timeout=8.0, follow_redirects=False, trust_env=False
        )
        self._owns_client = client is None
        self._login_lock = asyncio.Lock()
        self._tokens: dict[str, tuple[str, str]] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def credentials_ready(self, skill_id: UUID | None, config: SkillConnectionView) -> bool:
        # skill_id 为 None 表示技能尚不存在（如新技能草稿），走环境变量回退。
        if skill_id is not None and self._credentials is not None:
            status = await self._credentials.status(skill_id)
            if status.configured:
                return status.key_ready
        if config.username_ref and config.secret_ref:
            try:
                self._secrets.resolve(config.username_ref)
                self._secrets.resolve(config.secret_ref)
                return True
            except SecretNotFound:
                return False
        return False

    async def skill_get(
        self,
        connection_id: str,
        template: str,
        path: str,
        *,
        params: dict[str, str | int],
        auth: SkillLoginAuth | None = None,
        skill_id: UUID | None = None,
    ) -> object:
        config = await self._store.get(connection_id)
        if config is None or not config.enabled or template not in config.allowed_paths:
            raise SkillConnectionError("connection_disabled_or_path_denied")
        return await self._authorized_request(
            config,
            "GET",
            path,
            params=params,
            json_body=None,
            auth=auth,
            skill_id=skill_id,
            idempotency_key=None,
        )

    async def skill_write(
        self,
        connection_id: str,
        template: str,
        path: str,
        *,
        method: str,
        json_body: dict[str, object] | None,
        params: dict[str, str | int],
        auth: SkillLoginAuth | None = None,
        skill_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> object:
        """Execute a declarative write; only Admin-allowlisted write paths pass.

        幂等键透传为 Idempotency-Key 头，供支持幂等的远端去重；本地重放
        语义由计划步骤的唯一幂等键保证。
        """
        if method not in {"POST", "PUT", "PATCH", "DELETE"}:
            raise SkillConnectionError("write_method_not_allowed")
        config = await self._store.get(connection_id)
        if config is None or not config.enabled or template not in config.allowed_write_paths:
            raise SkillConnectionError("connection_disabled_or_write_path_denied")
        return await self._authorized_request(
            config,
            method,
            path,
            params=params,
            json_body=json_body,
            auth=auth,
            skill_id=skill_id,
            idempotency_key=idempotency_key,
        )

    async def _authorized_request(
        self,
        config: SkillConnectionView,
        method: str,
        path: str,
        *,
        params: dict[str, str | int],
        json_body: dict[str, object] | None,
        auth: SkillLoginAuth | None,
        skill_id: UUID | None,
        idempotency_key: str | None,
    ) -> object:
        headers: dict[str, str] = {}
        if config.auth_type == "login_bearer":
            if auth is None or auth.path not in config.allowed_auth_paths:
                raise SkillConnectionError("connection_auth_path_denied")
            headers["Authorization"] = f"Bearer {await self._login_token(config, auth, skill_id)}"
        elif config.secret_ref:
            try:
                secret = self._secrets.resolve(config.secret_ref)
            except SecretNotFound as error:
                raise SkillConnectionError("connection_secret_unavailable") from error
            if config.auth_type == "bearer":
                headers["Authorization"] = f"Bearer {secret}"
            elif config.header_name:
                headers[config.header_name] = secret
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            return await self._request(config, method, path, params, json_body, headers)
        except SkillConnectionError as error:
            if config.auth_type != "login_bearer" or error.reason_code != "connection_http_401":
                raise
        assert auth is not None
        cache_key = f"{config.id}:{skill_id or ''}"
        self._tokens.pop(cache_key, None)
        headers["Authorization"] = f"Bearer {await self._login_token(config, auth, skill_id)}"
        return await self._request(config, method, path, params, json_body, headers)

    async def _request(
        self,
        config: SkillConnectionView,
        method: str,
        path: str,
        params: dict[str, str | int],
        json_body: dict[str, object] | None,
        headers: dict[str, str],
    ) -> object:
        try:
            async with self._client.stream(
                method,
                config.base_url + path,
                params=params,
                json=json_body,
                headers=headers,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise SkillConnectionError("connection_redirect_denied")
                response.raise_for_status()
                payload = await self._read_json(response, max_bytes=64 * 1024)
                if isinstance(payload, dict) and payload.get("code") == 401:
                    raise SkillConnectionError("connection_http_401")
                return payload
        except httpx.HTTPStatusError as error:
            raise SkillConnectionError(f"connection_http_{error.response.status_code}") from error
        except (httpx.RequestError, ValueError) as error:
            raise SkillConnectionError("connection_request_failed") from error

    async def _login_token(
        self, config: SkillConnectionView, auth: SkillLoginAuth, skill_id: UUID | None
    ) -> str:
        stored: tuple[str, str, str] | None = None
        if self._credentials is not None and skill_id is not None:
            try:
                stored = await self._credentials.get(skill_id, config.id)
            except SkillCredentialError as error:
                raise SkillConnectionError(error.reason_code) from error
        if stored is not None:
            username, password, credential_version = stored
        else:
            try:
                username = self._secrets.resolve(config.username_ref or "")
                password = self._secrets.resolve(config.secret_ref or "")
            except SecretNotFound as error:
                raise SkillConnectionError("connection_secret_unavailable") from error
            credential_version = "env"
        cache_key = f"{config.id}:{skill_id or ''}"
        signature = (
            f"{config.base_url}|{config.secret_ref}|{config.username_ref}|"
            f"{credential_version}|{auth.model_dump_json()}"
        )
        async with self._login_lock:
            cached = self._tokens.get(cache_key)
            if cached is not None and cached[0] == signature:
                return cached[1]
            try:
                async with self._client.stream(
                    "POST",
                    config.base_url + auth.path,
                    json={auth.username_field: username, auth.password_field: password},
                    follow_redirects=False,
                ) as response:
                    if response.is_redirect or response.status_code >= 400:
                        raise SkillConnectionError("connection_login_rejected")
                    payload = await self._read_json(response, max_bytes=8 * 1024)
            except httpx.RequestError as error:
                raise SkillConnectionError("connection_request_failed") from error
            if not isinstance(payload, dict) or payload.get("code") not in (None, 200):
                raise SkillConnectionError("connection_login_rejected")
            token = payload.get(auth.token_field)
            if (
                not isinstance(token, str)
                or not token
                or len(token) > 4096
                or any(char.isspace() for char in token)
            ):
                raise SkillConnectionError("connection_login_invalid_response")
            self._tokens[cache_key] = (signature, token)
            return token

    @staticmethod
    async def _read_json(response: httpx.Response, *, max_bytes: int) -> object:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > max_bytes:
                raise SkillConnectionError("connection_response_too_large")
        try:
            return json.loads(content)
        except ValueError as error:
            raise SkillConnectionError("connection_invalid_response") from error
