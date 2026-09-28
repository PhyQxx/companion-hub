"""Validated, declarative Skill package contract."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_OPERATION = re.compile(r"^[a-z][a-z0-9_]*$")
_PARAM = re.compile(r"^[a-z][A-Za-z0-9_]*$")
_PATH_PARAM = re.compile(r"\{([a-z][A-Za-z0-9_]*)\}")
_LOGIN_PATH = re.compile(r"^/(?!/)[A-Za-z0-9_./-]+$")


class SkillParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["string", "integer", "number", "boolean"]
    required: bool = False
    location: Literal["path", "query", "body"] = "query"


class SkillOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=500)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=240)
    risk: Literal["read", "confirm"]
    parameters: dict[str, SkillParameter] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract(self) -> SkillOperation:
        if not _OPERATION.fullmatch(self.name):
            raise ValueError("invalid operation name")
        if not self.path.startswith("/") or self.path.startswith("//"):
            raise ValueError("operation path must be relative")
        if any(part in {".", ".."} for part in self.path.split("/")):
            raise ValueError("operation path traversal")
        if any(char in self.path for char in ("?", "#", "\\", "%")):
            raise ValueError("operation path contains forbidden characters")
        if self.risk == "read" and self.method != "GET":
            raise ValueError("read operation must use GET")
        if self.risk == "confirm" and self.method == "GET":
            raise ValueError("write operation cannot use GET")
        if len(self.parameters) > 12:
            raise ValueError("too many parameters")
        path_params = set(_PATH_PARAM.findall(self.path))
        if "{" in _PATH_PARAM.sub("", self.path) or "}" in _PATH_PARAM.sub("", self.path):
            raise ValueError("invalid path template")
        for name, param in self.parameters.items():
            if not _PARAM.fullmatch(name):
                raise ValueError("invalid parameter name")
            if param.location == "path" and (name not in path_params or not param.required):
                raise ValueError("path parameter must be required and present in path")
            if param.location == "body" and self.method == "GET":
                raise ValueError("GET operation cannot have body parameters")
        if path_params != {name for name, p in self.parameters.items() if p.location == "path"}:
            raise ValueError("path parameter declaration mismatch")
        return self


class SkillLoginAuth(BaseModel):
    """Public login contract; credentials belong to the admin-owned connection."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["login_bearer"]
    path: str = Field(min_length=1, max_length=240)
    username_field: str = Field(default="userName", min_length=1, max_length=64)
    password_field: str = Field(default="password", min_length=1, max_length=64)
    token_field: str = Field(default="token", min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_contract(self) -> SkillLoginAuth:
        if (
            not _LOGIN_PATH.fullmatch(self.path)
            or any(part in {".", ".."} for part in self.path.split("/"))
        ):
            raise ValueError("invalid_login_path")
        if not all(
            _PARAM.fullmatch(field)
            for field in (self.username_field, self.password_field, self.token_field)
        ) or self.username_field == self.password_field:
            raise ValueError("invalid_login_field")
        return self


class SkillApiManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    connection: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    auth: SkillLoginAuth | None = None
    operations: Annotated[list[SkillOperation], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def unique_names(self) -> SkillApiManifest:
        names = [item.name for item in self.operations]
        if len(set(names)) != len(names):
            raise ValueError("duplicate operation name")
        if self.connection == "pnkx":
            raise ValueError("pnkx_connection_reserved_use_admin_connection")
        return self


class SkillDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    instructions: str = Field(min_length=1, max_length=50_000)
    api: SkillApiManifest | None = None

    @model_validator(mode="after")
    def validate_name(self) -> SkillDocument:
        if not _NAME.fullmatch(self.name):
            raise ValueError("invalid skill name")
        return self
