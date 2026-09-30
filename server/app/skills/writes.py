"""Declarative Skill write operations, executed only via confirmed action plans.

写操作绝不作为聊天工具挂载（chat/service.py 将 ``skill_write`` 列为 runner
专用），只经 Action Registry 计划—确认—执行链路触发。执行闸门按数据库
实时状态复核技能/操作/连接并重新校验参数，绝不信任计划里缓存的旧契约。
"""

from __future__ import annotations

import logging
import re
from time import perf_counter
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from app.llm import ToolDefinition
from app.schemas import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult

from .connections import SkillConnectionError, SkillConnectionStore, SkillHttpClient
from .runtime import arguments_model_for
from .store import SkillStore

_SAFE_PATH_VALUE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
logger = logging.getLogger(__name__)


class _SkillWriteArgs(BaseModel):
    model_config = ConfigDict(extra="allow")

    skill_name: str
    operation: str


class SkillWriteToolHandler:
    """Generic executor behind ``skill.<skill_name>.<operation>`` A2 actions."""

    name = "skill_write"
    description = "执行已确认的技能写操作（仅供行动计划运行器调用）。"
    arguments_model: type[BaseModel] = _SkillWriteArgs
    max_privacy_level = PrivacyLevel.L1
    runs_local = False

    def __init__(
        self,
        store: SkillStore,
        connections: SkillConnectionStore,
        http_client: SkillHttpClient,
    ) -> None:
        self._store = store
        self._connections = connections
        self._http_client = http_client

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.arguments_model.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(_SkillWriteArgs, arguments)
        skill = await self._store.get_by_name(args.skill_name)
        if skill is None or not skill.enabled or skill.api is None:
            return await self._finish(False, started, None, "skill_disabled")
        operation = next(
            (item for item in skill.api.operations if item.name == args.operation),
            None,
        )
        if operation is None or operation.risk != "confirm" or operation.method == "GET":
            return await self._finish(False, started, None, "skill_operation_changed")
        meta = (skill.id, skill.version, skill.api.connection, operation.name)
        # 参数按实时契约重新校验：计划创建与执行之间技能可能已改版。
        try:
            values = (
                arguments_model_for(operation)
                .model_validate(args.model_dump(exclude={"skill_name", "operation"}))
                .model_dump(exclude_none=True)
            )
        except ValidationError:
            return await self._finish(False, started, meta, "skill_arguments_invalid")
        path = operation.path
        query: dict[str, str | int] = {}
        body: dict[str, object] = {}
        for name, value in values.items():
            spec = operation.parameters[name]
            if spec.location == "path":
                if not _SAFE_PATH_VALUE.fullmatch(str(value)):
                    return await self._finish(False, started, meta, "invalid_path_parameter")
                path = path.replace("{" + name + "}", quote(str(value), safe=""))
            elif spec.location == "body":
                body[name] = value
            else:
                query[name] = str(value).lower() if isinstance(value, bool) else value
        try:
            await self._http_client.skill_write(
                skill.api.connection,
                operation.path,
                path,
                method=operation.method,
                json_body=body or None,
                params=query,
                auth=skill.api.auth,
                skill_id=skill.id,
                idempotency_key=context.idempotency_key,
            )
        except SkillConnectionError as error:
            logger.warning(
                "skill write denied: skill=%s operation=%s reason=%s",
                args.skill_name,
                args.operation,
                error.reason_code,
            )
            return await self._finish(False, started, meta, error.reason_code)
        except Exception:
            logger.warning(
                "skill write execution failed skill=%s operation=%s",
                skill.name,
                operation.name,
                exc_info=True,
            )
            return await self._finish(False, started, meta, "skill_api_failed")
        return await self._finish(
            True,
            started,
            meta,
            data={"skill_name": skill.name, "operation": operation.name, "status": "ok"},
        )

    async def _finish(
        self,
        ok: bool,
        started: float,
        meta: tuple[UUID, int, str, str] | None,
        reason_code: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> ToolResult:
        latency = (perf_counter() - started) * 1_000
        if meta is not None:
            skill_id, skill_version, connection_id, operation_name = meta
            try:
                await self._store.record_run(
                    skill_id=skill_id,
                    skill_version=skill_version,
                    connection_id=connection_id,
                    operation=operation_name,
                    ok=ok,
                    reason_code=reason_code,
                    latency_ms=latency,
                )
            except Exception:
                logger.warning("skill run evidence could not be stored", exc_info=True)
        return ToolResult(
            ok=ok,
            tool_name=self.name,
            provider=meta[2] if meta else "skills",
            data=data or {},
            reason_code=reason_code,
            latency_ms=latency,
        )
