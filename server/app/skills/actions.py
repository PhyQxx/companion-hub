"""Sync declarative Skill write operations into the Action Registry (S3).

写动作固定 A2（每次确认）、L1 上限、回执验证，与 MCP 动作同一安全基线。
目录以数据库实时状态为准：技能停用/改版/删除后重新同步即被移除或覆盖。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model

from app.cognition.action_registry import (
    ActionDefinition,
    ActionRegistry,
    ActionRisk,
    ConfirmationPolicy,
    VerificationPolicy,
)
from app.schemas import PrivacyLevel

from .models import SkillOperation
from .runtime import _TYPES
from .store import SkillView

SKILL_ACTION_PREFIX = "skill."
SKILL_WRITE_TOOL_NAME = "skill_write"
SKILL_VERIFIER_ID = "skill.write_receipt"

_ACTION_ID = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)*$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BOUND = frozenset({"skill_name", "operation"})


@dataclass(frozen=True, slots=True)
class SkillActionSyncReport:
    registered: tuple[str, ...]
    removed: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]


def write_arguments_model(operation: SkillOperation) -> type[BaseModel] | None:
    """Per-operation strict arguments model; None when a parameter is unmodelable."""
    fields: dict[str, Any] = {}
    for name, spec in operation.parameters.items():
        if not _IDENTIFIER.fullmatch(name) or name in _BOUND:
            return None
        field_type = _TYPES[spec.type]
        fields[name] = (
            field_type if spec.required else field_type | None,
            ... if spec.required else None,
        )
    return create_model(
        "SkillWriteArgs_" + operation.name,
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def sync_skill_actions(
    registry: ActionRegistry, skills: Sequence[SkillView]
) -> SkillActionSyncReport:
    """Overwrite-sync ``skill.<name>.<operation>`` actions; stale ones removed."""
    desired: dict[str, tuple[ActionDefinition, type[BaseModel]]] = {}
    skipped: list[tuple[str, str]] = []
    for skill in skills:
        if not skill.enabled or skill.api is None:
            continue
        for operation in skill.api.operations:
            if operation.risk != "confirm":
                continue
            action_id = f"skill.{skill.name}.{operation.name}"
            if len(action_id) > 160 or _ACTION_ID.fullmatch(action_id) is None:
                skipped.append((action_id, "action_id_unsafe"))
                continue
            if action_id in desired:
                skipped.append((action_id, "action_id_collision"))
                continue
            model = write_arguments_model(operation)
            if model is None:
                skipped.append((action_id, "schema_unsupported"))
                continue
            desired[action_id] = (
                ActionDefinition(
                    action_id=action_id,
                    label=f"技能 · {skill.name}.{operation.name}"[:160],
                    description=(
                        f"已启用技能 {skill.name} 的写操作：{operation.description}。"
                        "外部 HTTP 写入不可自动回滚，每次执行前都需用户确认，"
                        "结果以回执验证。"
                    )[:500],
                    risk=ActionRisk.A2_CONFIRM,
                    confirmation_policy=ConfirmationPolicy.ALWAYS,
                    reversible=False,
                    tool_name=SKILL_WRITE_TOOL_NAME,
                    arguments_schema=model.model_json_schema(),
                    bound_arguments={"skill_name": skill.name, "operation": operation.name},
                    timeout_seconds=30,
                    verification_policy=VerificationPolicy.RECEIPT,
                    verifier_id=SKILL_VERIFIER_ID,
                    max_privacy_level=PrivacyLevel.L1,
                ),
                model,
            )
    removed = tuple(
        definition.action_id
        for definition in registry.definitions()
        if definition.action_id.startswith(SKILL_ACTION_PREFIX)
        and definition.action_id not in desired
    )
    for action_id in removed:
        registry.remove(action_id)
    for _action_id, (definition, model) in desired.items():
        registry.upsert(definition, model)
    registry.validate()
    return SkillActionSyncReport(
        registered=tuple(sorted(desired)),
        removed=removed,
        skipped=tuple(skipped),
    )
