"""技能写链路巡检：自治修复的第一环——发现配置漂移并生成管理建议。

只做数据库内省（manifest 参数、连接白名单、启用状态），不发外部请求。
发现项落入 skill_suggestion（dedupe 去重），由 Admin 在建议流里处置——
诊断自动、修复留人。2026-09-30 连续多轮的「空参数写操作 / 写路径未
放行」两类故障，由此可在无人盯守时自动浮出水面。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from uuid import UUID

from .connections import SkillConnectionStore
from .store import SkillStore, SkillView

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 6 * 3600.0


@dataclass(frozen=True, slots=True)
class AuditFinding:
    skill_id: UUID
    skill_version: int
    operation: str
    reason_code: str
    kind: str
    title: str
    guidance: str


def audit_skill(
    skill: SkillView, connection_enabled: bool, write_paths: frozenset[str]
) -> list[AuditFinding]:
    """单个技能的写链路体检：连接 → 写路径 → 参数，按严重度短路。"""
    if skill.api is None:
        return []
    findings: list[AuditFinding] = []
    if not connection_enabled:
        return [
            AuditFinding(
                skill_id=skill.id,
                skill_version=skill.version,
                operation="",
                reason_code="audit_connection_missing",
                kind="connection",
                title=f"技能 {skill.name} 的连接不可用",
                guidance=(
                    f"连接 {skill.api.connection} 缺失或停用，该技能读写均无法执行；"
                    "请在 Admin 技能中心的连接管理中启用并配置凭据。"
                ),
            )
        ]
    for operation in skill.api.operations:
        if operation.risk != "confirm":
            continue
        if not operation.parameters:
            findings.append(
                AuditFinding(
                    skill_id=skill.id,
                    skill_version=skill.version,
                    operation=operation.name,
                    reason_code="audit_write_op_without_params",
                    kind="contract",
                    title=f"{skill.name}.{operation.name} 未声明参数",
                    guidance=(
                        "该写操作参数为空，动作无法携带内容（如日记标题/正文），"
                        "模型即使起草也会被校验拒绝。请修订技能 manifest，"
                        "为写操作补齐 body 参数。"
                    ),
                )
            )
        if operation.path not in write_paths:
            findings.append(
                AuditFinding(
                    skill_id=skill.id,
                    skill_version=skill.version,
                    operation=operation.name,
                    reason_code="audit_write_path_not_allowlisted",
                    kind="connection",
                    title=f"{skill.name}.{operation.name} 写路径未放行",
                    guidance=(
                        f"路径 {operation.path} 不在连接 {skill.api.connection} 的"
                        "写白名单里，写入会被 connection_disabled_or_write_path_denied "
                        "拒绝。请管理员在连接配置中放行该路径。"
                    ),
                )
            )
    return findings


class SkillAuditScheduler:
    """周期巡检：首扫延迟一个启动宽限期，之后按间隔轮询。

    首扫不立即执行——进程启动即写库会在 SQLite :memory: 测试环境里与
    短暂请求事务并发（同 TaskScheduler 的约定）；60s 宽限期对生产足够快。
    """

    def __init__(
        self,
        store: SkillStore,
        connections: SkillConnectionStore | None = None,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        initial_delay_seconds: float = 60.0,
    ) -> None:
        self._store = store
        self._connections = connections
        self._interval = interval_seconds
        self._initial_delay = initial_delay_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="aria-skill-audit")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        await asyncio.sleep(self._initial_delay)
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.warning("skill audit sweep failed", exc_info=True)
            await asyncio.sleep(self._interval)

    async def run_once(self) -> int:
        """执行一轮巡检并落建议，返回本轮新增建议数。"""
        created = 0
        for skill in await self._store.list(enabled_only=True):
            connection = (
                await self._connections.get(skill.api.connection)
                if self._connections is not None and skill.api is not None
                else None
            )
            findings = audit_skill(
                skill,
                connection_enabled=connection is not None and connection.enabled,
                write_paths=frozenset(connection.allowed_write_paths if connection else ()),
            )
            for finding in findings:
                stored = await self._store.record_audit_suggestion(
                    skill_id=finding.skill_id,
                    skill_version=finding.skill_version,
                    operation=finding.operation,
                    reason_code=finding.reason_code,
                    kind=finding.kind,
                    title=finding.title,
                    guidance=finding.guidance,
                )
                if stored:
                    created += 1
                    logger.warning("skill audit: %s（%s）", finding.title, finding.reason_code)
        if created:
            logger.warning("skill audit sweep: %d 条新建议待管理端处置", created)
        return created
