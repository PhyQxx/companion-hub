"""FLOW-01 流程存储：CRUD 与用户内名称唯一。

名称在用户内做 casefold 唯一裁决（数据库唯一约束用原值，大小写变体
由应用层拒绝），同名保存需显式删除旧流程后重建。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import Database, WorkflowRecord
from app.ids import uuid7

from .models import WorkflowStep, WorkflowView

MAX_WORKFLOW_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 500
MAX_WORKFLOW_STEPS = 10


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _to_view(record: WorkflowRecord) -> WorkflowView:
    return WorkflowView(
        id=record.id,
        user_id=record.user_id,
        name=record.name,
        description=record.description,
        steps=[WorkflowStep.model_validate(item) for item in record.steps or []],
        created_at=_aware(record.created_at),
        updated_at=_aware(record.updated_at),
    )


class WorkflowStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    async def create_workflow(
        self,
        *,
        user_id: UUID,
        name: str,
        steps: list[WorkflowStep],
        description: str | None = None,
        now: datetime | None = None,
    ) -> WorkflowView:
        cleaned_name = _clean_name(name)
        moment = now or datetime.now(UTC)
        record = WorkflowRecord(
            id=uuid7(),
            user_id=user_id,
            name=cleaned_name,
            description=description,
            steps=[item.model_dump(mode="json") for item in steps],
            created_at=moment,
            updated_at=moment,
        )
        try:
            async with self._database.sessions.begin() as session:
                session.add(record)
        except IntegrityError as error:
            raise ValueError(f"同名流程已存在：{cleaned_name}") from error
        return _to_view(record)

    async def get_workflow(self, user_id: UUID, workflow_id: UUID) -> WorkflowRecord:
        async with self._database.sessions() as session:
            record = await session.get(WorkflowRecord, workflow_id)
        if record is None or record.user_id != user_id:
            raise LookupError("workflow not found")
        return record

    async def get_workflow_view(self, user_id: UUID, workflow_id: UUID) -> WorkflowView:
        return _to_view(await self.get_workflow(user_id, workflow_id))

    async def find_by_name(self, user_id: UUID, name: str) -> WorkflowView | None:
        keyword = _clean_name(name).casefold()
        if not keyword:
            return None
        for record in await self._user_records(user_id):
            if record.name.casefold() == keyword:
                return _to_view(record)
        return None

    async def list_workflows(self, user_id: UUID, *, limit: int = 100) -> list[WorkflowView]:
        query = (
            select(WorkflowRecord)
            .where(WorkflowRecord.user_id == user_id)
            .order_by(WorkflowRecord.name)
            .limit(limit)
        )
        async with self._database.sessions() as session:
            records = (await session.execute(query)).scalars().all()
        return [_to_view(record) for record in records]

    async def delete_workflow(self, user_id: UUID, workflow_id: UUID) -> None:
        await self.get_workflow(user_id, workflow_id)
        async with self._database.sessions.begin() as session:
            managed = await session.get(WorkflowRecord, workflow_id)
            assert managed is not None
            await session.delete(managed)

    async def _user_records(self, user_id: UUID) -> list[WorkflowRecord]:
        query = (
            select(WorkflowRecord)
            .where(WorkflowRecord.user_id == user_id)
            .order_by(WorkflowRecord.name)
        )
        async with self._database.sessions() as session:
            return list((await session.execute(query)).scalars().all())


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned or len(cleaned) > MAX_WORKFLOW_NAME_CHARS:
        raise ValueError("流程名称必须为 1-120 字符")
    return cleaned
