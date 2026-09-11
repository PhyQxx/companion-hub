"""远端 JSON Schema → 保守 Pydantic 模型的唯一构建点（MCP-C2/D 共用）。

远端 Schema 是不可信数据：只接受对象 + string/number/integer/boolean 标量
属性 + 字符串 enum，其余结构一律返回 None。生成的模型同时决定：
- 动作注册表的动态参数（actions.py 再包一层 {"arguments": ...}）；
- 聊天挂载的工具参数模型（chat_tools.py 直接使用扁平形状）。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

_MAX_PROPERTIES = 16
_MAX_STRING_CHARS = 2_000
_NUMERIC_BOUND = 1_000_000_000
_SUPPORTED_TYPES = {"string", "number", "integer", "boolean"}
_REJECTED_COMPOSITES = ("anyOf", "oneOf", "allOf", "$ref", "$defs", "definitions", "not")
_PROPERTY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def build_flat_arguments_model(schema: Any) -> type[BaseModel] | None:
    """从远端 JSON Schema 保守生成扁平参数模型；不支持的结构返回 None。"""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return None
    if any(key in schema for key in _REJECTED_COMPOSITES):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties or len(properties) > _MAX_PROPERTIES:
        return None
    required = schema.get("required", [])
    if not isinstance(required, list) or len(set(required)) != len(required):
        return None
    if not set(required).issubset(properties):
        return None
    fields: dict[str, Any] = {}
    for name, spec in properties.items():
        if not isinstance(name, str) or _PROPERTY_NAME.match(name) is None:
            return None
        if not isinstance(spec, dict) or spec.get("type") not in _SUPPORTED_TYPES:
            return None
        annotation = _annotation(spec)
        if annotation is None:
            return None
        fields[name] = (annotation, _field_spec(spec, name in required))
    return create_model(
        "McpArguments",
        __config__=ConfigDict(extra="forbid", frozen=True),
        **fields,
    )


def wrap_nested(model: type[BaseModel]) -> type[BaseModel]:
    """把扁平模型包成 {"arguments": ...}——动作编译与绑定参数合并所需。"""
    return create_model(
        "McpArgumentsNested",
        __config__=ConfigDict(extra="forbid", frozen=True),
        arguments=(model, Field(...)),
    )


def _annotation(spec: dict[str, Any]) -> Any:
    """单属性注解：字符串（可选 enum）/数值/布尔；不支持的 enum 返回 None。"""
    prop_type = spec["type"]
    if prop_type == "string":
        enum = spec.get("enum")
        if enum is None:
            return str
        if (
            not isinstance(enum, list)
            or not enum
            or len(enum) > 32
            or len(set(enum)) != len(enum)
            or not all(isinstance(item, str) and 0 < len(item) <= 64 for item in enum)
        ):
            return None
        return Literal[tuple(enum)]
    if prop_type == "integer":
        return int
    if prop_type == "number":
        return float
    return bool


def _field_spec(spec: dict[str, Any], required: bool) -> Any:
    """必填/可选 + 体积钳制：字符串 ≤2000 字符，数值边界取远端与硬上限交集。"""
    default = ... if required else None
    prop_type = spec["type"]
    if prop_type == "string" and spec.get("enum") is None:
        max_length = spec.get("maxLength")
        bound = (
            min(int(max_length), _MAX_STRING_CHARS)
            if isinstance(max_length, int)
            else _MAX_STRING_CHARS
        )
        return Field(default, max_length=max(1, bound))
    if prop_type in {"integer", "number"}:
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        ge = (
            max(float(minimum), -_NUMERIC_BOUND)
            if isinstance(minimum, (int, float))
            else -_NUMERIC_BOUND
        )
        le = (
            min(float(maximum), _NUMERIC_BOUND)
            if isinstance(maximum, (int, float))
            else _NUMERIC_BOUND
        )
        return Field(default, ge=ge, le=le)
    return Field(default)
