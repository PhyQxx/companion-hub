"""产品级数据导出/导入：换机与跨版本迁移的可读 JSON 档案。

边界（v1，FR-S3 后续批次）：
- 只导出「伴侣数据」——用户、人格版本、对话、消息、记忆（含来源）、时间线、
  任务、承诺、联系人、日历、简报/回顾、会议、流程与家庭场景；
- 凭据与机器状态绝不导出：登录凭据/会话、设备注册与命令、推送订阅、
  OAuth 刷新令牌、配置中心（含 secret）、运行时租约、任务引擎、资产库；
- 导入只支持恢复到空库（目标已有对话/记忆数据即拒绝），全程单事务、
  逐表主键冲突即整体回滚；导入不自动发布人格或触发任何投递。

导出文件包含全部 L2 私密内容，敏感度等同数据库备份（见 docs/07 BK-01）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.sql.elements import KeyedColumnElement

from app.db import (
    AppUserRecord,
    Base,
    CalendarEventRecord,
    CognitiveGoalRecord,
    ContactRecord,
    ConversationRecord,
    DailyBriefRecord,
    DailyReviewRecord,
    Database,
    HomeSceneRecord,
    MeetingRecord,
    MemoryRecord,
    MemorySourceRecord,
    MessageRecord,
    PersonaVersionRecord,
    TaskItemRecord,
    TimelineEventRecord,
    WorkflowRecord,
)
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard

# 导入按外键依赖排序；导出顺序无关，复用同一列表
EXPORT_TABLES: tuple[type[Base], ...] = (
    AppUserRecord,
    PersonaVersionRecord,
    ConversationRecord,
    MessageRecord,
    MemoryRecord,
    MemorySourceRecord,
    TimelineEventRecord,
    TaskItemRecord,
    CognitiveGoalRecord,
    ContactRecord,
    CalendarEventRecord,
    DailyBriefRecord,
    DailyReviewRecord,
    MeetingRecord,
    WorkflowRecord,
    HomeSceneRecord,
)

# 目标库这些表已有数据即拒绝导入（空库恢复语义）
EMPTINESS_GUARD_TABLES: tuple[type[Base], ...] = (
    MessageRecord,
    MemoryRecord,
    TaskItemRecord,
)


class ExportArchive(StrictModel):
    format: Literal["aria-export"] = "aria-export"
    schema_version: int = 1
    hub_version: str
    exported_at: datetime
    tables: dict[str, list[dict[str, Any]]]


class ImportSummary(StrictModel):
    inserted: dict[str, int]
    skipped_tables: list[str]


class ImportRequest(StrictModel):
    archive: ExportArchive
    confirm: bool


def _dump_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


def _coerce_value(column: KeyedColumnElement[Any], value: Any) -> Any:
    if value is None:
        return None
    python_type = column.type.python_type
    if python_type is UUID:
        return UUID(str(value))
    if python_type is datetime:
        return datetime.fromisoformat(str(value))
    if python_type is date:
        return date.fromisoformat(str(value))
    return value


def _column_pairs(model: type[Base]) -> list[tuple[str, str, KeyedColumnElement[Any]]]:
    """(属性名, 列名, 列)：属性名用于 ORM 读写，列名用于档案字段。"""
    return [(column.key, column.name, column) for column in model.__table__.columns]


def create_admin_export_router(
    *,
    database: Database,
    admin_token: str | None,
    hub_version: str = "dev",
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/data",
        tags=["admin-data"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("/export", response_model=ExportArchive)
    async def export_archive() -> ExportArchive:
        tables: dict[str, list[dict[str, Any]]] = {}
        async with database.sessions() as session:
            for model in EXPORT_TABLES:
                pairs = _column_pairs(model)
                records = (await session.scalars(select(model))).all()
                rows = [
                    {
                        column_name: _dump_value(getattr(record, attribute_name))
                        for attribute_name, column_name, _column in pairs
                    }
                    for record in records
                ]
                tables[model.__tablename__] = rows
        return ExportArchive(
            hub_version=hub_version,
            exported_at=datetime.now(UTC),
            tables=tables,
        )

    @router.post("/import", response_model=ImportSummary)
    async def import_archive(body: ImportRequest) -> ImportSummary:
        if not body.confirm:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="导入会写入目标库，必须显式 confirm=true",
            )
        archive = body.archive
        if archive.schema_version != 1:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"不支持的导出格式版本：{archive.schema_version}",
            )
        inserted: dict[str, int] = {}
        skipped: list[str] = []
        async with database.sessions.begin() as session:
            for guard in EMPTINESS_GUARD_TABLES:
                existing = (await session.scalars(select(guard).limit(1))).first()
                if existing is not None:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        detail=(
                            f"目标库的 {guard.__tablename__} 已有数据；"
                            "导入仅支持恢复到空库，请先在新实例上执行"
                        ),
                    )
            for model in EXPORT_TABLES:
                table_name = model.__tablename__
                rows = archive.tables.get(table_name)
                if rows is None:
                    skipped.append(table_name)
                    continue
                pairs = _column_pairs(model)
                for row in rows:
                    kwargs = {
                        attribute_name: _coerce_value(column, row.get(column_name))
                        for attribute_name, column_name, column in pairs
                        if column_name in row
                    }
                    session.add(model(**kwargs))
                inserted[table_name] = len(rows)
        return ImportSummary(inserted=inserted, skipped_tables=skipped)

    return router
