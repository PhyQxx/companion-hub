"""Offline request compilation shared by runtime execution and fixture replay."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from .models import SkillOperation

_SAFE_PATH_VALUE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TYPES: dict[str, type[Any]] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def arguments_model_for(operation: SkillOperation) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for name, parameter in operation.parameters.items():
        field_type = _TYPES[parameter.type]
        fields[name] = (
            field_type if parameter.required else field_type | None,
            ... if parameter.required else None,
        )
    return create_model(
        "SkillArgs_" + operation.name,
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


@dataclass(frozen=True)
class PreparedSkillRequest:
    path: str
    query: dict[str, Any]
    body: dict[str, object]


def prepare_request(operation: SkillOperation, arguments: dict[str, Any]) -> PreparedSkillRequest:
    try:
        values = (
            arguments_model_for(operation).model_validate(arguments).model_dump(exclude_none=True)
        )
    except ValidationError as error:
        raise ValueError("skill_arguments_invalid") from error
    path = operation.path
    query: dict[str, Any] = {}
    body: dict[str, object] = {}
    for name, value in values.items():
        parameter = operation.parameters[name]
        if parameter.location == "path":
            if not _SAFE_PATH_VALUE.fullmatch(str(value)):
                raise ValueError("invalid_path_parameter")
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        elif parameter.location == "body":
            body[name] = value
        else:
            query[name] = str(value).lower() if isinstance(value, bool) else value
    return PreparedSkillRequest(path, query, body)
