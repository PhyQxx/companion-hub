"""SQL adapter for media authority and owned provider operation execution."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import select

from app.config.models import RunBudgetConfig
from app.db import Database, TaskRunRecord
from app.harness.budget import BudgetDenied
from app.harness.operations import GenerationTicket, OperationPolicy
from app.schemas import PrivacyLevel

from .operation import operate_with_run


def _source_fingerprint(row: TaskRunRecord) -> str:
    return hashlib.sha256(
        json.dumps(
            {"contract": row.contract, "privacy": row.privacy_level},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class SqlCapabilityExecution:
    def __init__(
        self, database: Database, *, budget_source: Callable[[], RunBudgetConfig] | None = None
    ) -> None:
        self._database = database
        self._budget_source = budget_source

    async def ticket(self, user_id: UUID, task_id: str) -> GenerationTicket | None:
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(TaskRunRecord)
                .where(
                    TaskRunRecord.user_id == user_id,
                    TaskRunRecord.contract["provider_task_id"].as_string() == task_id,
                    TaskRunRecord.contract["entry"].as_string() == "capability.video.submit",
                    TaskRunRecord.status == "succeeded",
                )
                .order_by(TaskRunRecord.created_at.desc(), TaskRunRecord.id.desc())
                .limit(1)
            )
        if row is None:
            return None
        fingerprint = row.contract.get("endpoint_fingerprint")
        if not isinstance(fingerprint, str):
            raise BudgetDenied("generation_task_source_changed")
        return GenerationTicket(
            row.id, user_id, PrivacyLevel(row.privacy_level), fingerprint, _source_fingerprint(row)
        )

    async def validate_ticket(self, ticket: GenerationTicket) -> None:
        async with self._database.sessions() as session:
            row = await session.get(TaskRunRecord, ticket.id)
            if (
                row is None
                or row.user_id != ticket.user_id
                or row.status != "succeeded"
                or _source_fingerprint(row) != ticket.source_fingerprint
            ):
                raise BudgetDenied("generation_task_source_changed")

    async def execute(
        self,
        policy: OperationPolicy,
        *,
        user_id: UUID,
        privacy_level: PrivacyLevel,
        entry: str,
        invoke: Callable[[Callable[[], Awaitable[None]]], Awaitable[object]],
        evidence: Callable[[object], dict[str, str]],
        source_guard: Callable[[], Awaitable[None]],
        cost_endpoint: str,
    ) -> object:
        return await operate_with_run(
            self._database,
            policy,
            user_id=user_id,
            privacy_level=privacy_level,
            entry=entry,
            invoke=invoke,
            evidence=evidence,
            source_guard=source_guard,
            cost_endpoint=cost_endpoint,
            budget_source=self._budget_source,
        )
