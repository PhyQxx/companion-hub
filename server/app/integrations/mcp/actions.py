"""MCP-D：把白名单写工具同步进 Action Registry（计划—确认—执行专用）。

远端 Schema 是不可信数据：只接受保守子集（对象 + string/number/integer/boolean
标量属性 + 字符串 enum），其余结构一律跳过并给出原因，绝不动态执行远端逻辑。
所有 MCP 写动作固定 A2（每次确认）、不可逆、L1 上限、回执验证——本地策略权威，
远端 readOnlyHint 只作目录参考。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.cognition.action_registry import (
    ActionDefinition,
    ActionRegistry,
    ActionRisk,
    ConfirmationPolicy,
    VerificationPolicy,
)
from app.schemas import PrivacyLevel

from .manager import McpManager

MCP_ACTION_PREFIX = "mcp."
MCP_TOOL_NAME = "mcp_tool_call"
MCP_BOUND_ARGUMENT = "tool"
MCP_VERIFIER_ID = "mcp.call_receipt"

_MAX_PROPERTIES = 16
_MAX_STRING_CHARS = 2_000
_NUMERIC_BOUND = 1_000_000_000
_SUPPORTED_TYPES = {"string", "number", "integer", "boolean"}
_REJECTED_COMPOSITES = ("anyOf", "oneOf", "allOf", "$ref", "$defs", "definitions", "not")
_PROPERTY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ACTION_ID = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)*$")


@dataclass(frozen=True, slots=True)
class McpActionSyncReport:
    registered: tuple[str, ...]
    removed: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]


def build_arguments_model(schema: Any) -> type[BaseModel] | None:
    """从远端 JSON Schema 保守生成参数模型；不支持的结构返回 None。

    生成形状固定为 {"arguments": {远端标量属性}}——动作编译会把绑定参数
    `tool` 与该模型合并为扁平 tool_arguments，嵌套一层让执行器仍能整体校验。
    """
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
    inner = create_model(
        "McpArgumentsItem",
        __config__=ConfigDict(extra="forbid", frozen=True),
        **fields,
    )
    return create_model(
        "McpArguments",
        __config__=ConfigDict(extra="forbid", frozen=True),
        arguments=(inner, Field(...)),
    )


def sync_mcp_actions(registry: ActionRegistry, manager: McpManager) -> McpActionSyncReport:
    """把目录中的写工具覆盖式同步进注册表；目录消失的动作被移除。"""
    desired: dict[str, tuple[ActionDefinition, type[BaseModel]]] = {}
    skipped: list[tuple[str, str]] = []
    for tool in manager.catalog():
        if tool.read_only:
            continue
        action_id = _normalize_action_id(tool.internal_name)
        if action_id is None:
            skipped.append((tool.internal_name, "action_id_unsafe"))
            continue
        if action_id in desired:
            skipped.append((tool.internal_name, "action_id_collision"))
            continue
        model = build_arguments_model(tool.input_schema)
        if model is None:
            skipped.append((tool.internal_name, "schema_unsupported"))
            continue
        if MCP_BOUND_ARGUMENT in model.model_fields:
            skipped.append((tool.internal_name, "bound_argument_collision"))
            continue
        desired[action_id] = (
            ActionDefinition(
                action_id=action_id,
                label=f"MCP · {tool.title or tool.remote_name}"[:160],
                description=(
                    f"外部 MCP 工具（{tool.server_id} · {tool.remote_name}）。"
                    "写操作每次执行前都需用户确认，结果以回执验证。"
                )[:500],
                risk=ActionRisk.A2_CONFIRM,
                confirmation_policy=ConfirmationPolicy.ALWAYS,
                reversible=False,
                tool_name=MCP_TOOL_NAME,
                arguments_schema=model.model_json_schema(),
                bound_arguments={MCP_BOUND_ARGUMENT: tool.internal_name},
                timeout_seconds=_clamp_timeout(manager.server_config(tool.server_id)),
                verification_policy=VerificationPolicy.RECEIPT,
                verifier_id=MCP_VERIFIER_ID,
                max_privacy_level=PrivacyLevel.L1,
            ),
            model,
        )
    removed = tuple(
        definition.action_id
        for definition in registry.definitions()
        if definition.action_id.startswith(MCP_ACTION_PREFIX)
        and definition.action_id not in desired
    )
    for action_id in removed:
        registry.remove(action_id)
    for _action_id, (definition, model) in desired.items():
        registry.upsert(definition, model)
    registry.validate()
    return McpActionSyncReport(
        registered=tuple(sorted(desired)),
        removed=removed,
        skipped=tuple(skipped),
    )


def _normalize_action_id(internal_name: str) -> str | None:
    """internal_name 形如 mcp.{server}.{remote}；段内字符归一到 TokenName 允许集。"""
    parts = internal_name.split(".")
    if len(parts) != 3:
        return None
    normalized = [parts[0].lower()] + [
        re.sub(r"[^a-z0-9_-]", "-", part.lower()).strip("-") for part in parts[1:]
    ]
    if not all(normalized):
        return None
    candidate = ".".join(normalized)
    if len(candidate) > 160 or _ACTION_ID.match(candidate) is None:
        return None
    return candidate


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


def _clamp_timeout(config: Any) -> int:
    value = int(getattr(config, "call_timeout_seconds", 15))
    return max(1, min(300, value))
