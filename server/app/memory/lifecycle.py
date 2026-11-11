"""Owned, transactional lifecycle state; never rewrites semantic revisions."""

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm.attributes import flag_modified

from app.db import Database
from app.db.claims import assert_current_claim

from .lifecycle_policy import POLICY_VERSION, MemoryTier, aware, evaluate
from .persistence import MemoryLifecycleRecord, MemoryRecord, MemoryUsageRecord

POINTS = {"used_in_answer": 1, "used_in_action": 2, "user_reaffirmed": 2, "user_confirmed": 3}


class LifecycleConflict(ValueError):
    pass


class MemoryLifecycle:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def use(
        self,
        memory_id: int,
        *,
        user_id: UUID,
        turn_key: str,
        kind: str,
        timezone: str = "UTC",
        now: datetime | None = None,
    ) -> None:
        if kind not in POINTS or not 1 <= len(turn_key) <= 160:
            raise ValueError("invalid_usage_event")
        moment = aware(now or datetime.now(UTC))
        day = moment.astimezone(ZoneInfo(timezone)).date()
        async with self.database.sessions.begin() as session:
            await assert_current_claim(session)
            row = await session.scalar(
                update(MemoryRecord)
                .where(
                    MemoryRecord.id == memory_id,
                    MemoryRecord.user_id == user_id,
                    MemoryRecord.status == "active",
                )
                .values(updated_at=MemoryRecord.updated_at)
                .returning(MemoryRecord)
            )
            if row is None:
                raise LookupError("memory not found")
            flag_modified(row, "updated_at")
            if row.valid_to and aware(row.valid_to) <= moment:
                return
            event = await session.scalar(
                select(MemoryUsageRecord).where(
                    MemoryUsageRecord.memory_id == memory_id, MemoryUsageRecord.turn_key == turn_key
                )
            )
            if event is not None:
                if event.points >= POINTS[kind]:
                    return
                event.points, event.kind = POINTS[kind], kind
            else:
                session.add(
                    MemoryUsageRecord(
                        memory_id=memory_id,
                        user_id=user_id,
                        turn_key=turn_key,
                        kind=kind,
                        points=POINTS[kind],
                        day=day.isoformat(),
                        occurred_at=moment,
                    )
                )
                row.use_count += 1
            await session.flush()
            daily = list(
                (
                    await session.execute(
                        select(MemoryUsageRecord.day, func.sum(MemoryUsageRecord.points))
                        .where(
                            MemoryUsageRecord.memory_id == memory_id,
                            MemoryUsageRecord.day >= (day - timedelta(days=89)).isoformat(),
                            MemoryUsageRecord.day <= day.isoformat(),
                        )
                        .group_by(MemoryUsageRecord.day)
                    )
                ).all()
            )
            recent = [
                (d, min(3, p)) for d, p in daily if d >= (day - timedelta(days=29)).isoformat()
            ]
            decision = evaluate(
                tier=MemoryTier(row.tier),
                changed_at=row.tier_changed_at,
                created_at=row.created_at,
                last_used_at=row.last_used_at,
                importance=row.importance,
                locked=row.tier_locked,
                now=moment,
                used=True,
                promote=event is None and os.getenv("ARIA_MEMORY_AUTO_PROMOTION", "1") == "1",
                eligible=row.confidence is None or row.confidence >= 0.6,
                score30=sum(p for _, p in recent),
                days30=len(recent),
                score90=sum(min(3, p) for _, p in daily),
                days90=len(daily),
            )
            if decision.tier != row.tier:
                session.add(
                    MemoryLifecycleRecord(
                        memory_id=row.id,
                        user_id=user_id,
                        old_tier=row.tier,
                        new_tier=decision.tier,
                        reason=decision.reason,
                        policy_version=POLICY_VERSION,
                        occurred_at=moment,
                    )
                )
                row.tier, row.tier_changed_at = decision.tier.value, moment
            row.last_used_at = max(aware(row.last_used_at or moment), moment)
            row.next_decay_at = decision.next_decay_at
            row.lifecycle_version += 1
            flag_modified(row, "updated_at")

    async def set_tier(
        self,
        memory_id: int,
        *,
        user_id: UUID,
        tier: MemoryTier,
        locked: bool,
        expected_version: int,
    ) -> None:
        now = datetime.now(UTC)
        async with self.database.sessions.begin() as session:
            row = await session.scalar(
                update(MemoryRecord)
                .where(MemoryRecord.id == memory_id, MemoryRecord.user_id == user_id)
                .values(updated_at=MemoryRecord.updated_at)
                .returning(MemoryRecord)
            )
            if row is None:
                raise LookupError("memory not found")
            flag_modified(row, "updated_at")
            if row.lifecycle_version != expected_version:
                raise LifecycleConflict("lifecycle_version_changed")
            session.add(
                MemoryLifecycleRecord(
                    memory_id=row.id,
                    user_id=user_id,
                    old_tier=row.tier,
                    new_tier=tier,
                    reason="manual_locked" if locked else "manual",
                    policy_version=POLICY_VERSION,
                    occurred_at=now,
                )
            )
            row.tier, row.tier_locked, row.tier_changed_at = tier.value, locked, now
            row.next_decay_at = None
            row.lifecycle_version += 1
            flag_modified(row, "updated_at")

    async def decay_batch(self, *, now: datetime | None = None, limit: int = 100) -> int:
        moment = aware(now or datetime.now(UTC))
        async with self.database.sessions.begin() as session:
            ids = list(
                await session.scalars(
                    select(MemoryRecord.id)
                    .where(
                        MemoryRecord.status == "active",
                        MemoryRecord.tier_locked.is_(False),
                        MemoryRecord.tier != "dormant",
                        or_(
                            MemoryRecord.next_decay_at.is_(None),
                            MemoryRecord.next_decay_at <= moment,
                        ),
                    )
                    .order_by(MemoryRecord.id)
                    .limit(limit)
                )
            )
            changed = 0
            for identifier in ids:
                row = await session.scalar(
                    update(MemoryRecord)
                    .where(
                        MemoryRecord.id == identifier,
                        MemoryRecord.status == "active",
                        MemoryRecord.tier_locked.is_(False),
                    )
                    .values(updated_at=MemoryRecord.updated_at)
                    .returning(MemoryRecord)
                )
                if row is None:
                    continue
                flag_modified(row, "updated_at")
                decision = evaluate(
                    tier=MemoryTier(row.tier),
                    changed_at=row.tier_changed_at,
                    created_at=row.created_at,
                    last_used_at=row.last_used_at,
                    importance=row.importance,
                    locked=False,
                    now=moment,
                )
                if decision.tier != row.tier:
                    session.add(
                        MemoryLifecycleRecord(
                            memory_id=row.id,
                            user_id=row.user_id,
                            old_tier=row.tier,
                            new_tier=decision.tier,
                            reason=decision.reason,
                            policy_version=POLICY_VERSION,
                            occurred_at=moment,
                        )
                    )
                    row.tier, row.tier_changed_at = decision.tier.value, moment
                    changed += 1
                row.next_decay_at = decision.next_decay_at
                row.lifecycle_version += 1
                flag_modified(row, "updated_at")
            return changed

    async def history(self, memory_id: int, *, user_id: UUID) -> list[dict[str, object]]:
        async with self.database.sessions() as session:
            owned = await session.scalar(
                select(MemoryRecord.id).where(
                    MemoryRecord.id == memory_id, MemoryRecord.user_id == user_id
                )
            )
            if owned is None:
                raise LookupError("memory not found")
            rows = list(
                await session.scalars(
                    select(MemoryLifecycleRecord)
                    .where(
                        MemoryLifecycleRecord.memory_id == memory_id,
                        MemoryLifecycleRecord.user_id == user_id,
                    )
                    .order_by(MemoryLifecycleRecord.id.desc())
                    .limit(100)
                )
            )
            return [
                {
                    "old_tier": row.old_tier,
                    "new_tier": row.new_tier,
                    "reason": row.reason,
                    "policy_version": row.policy_version,
                    "occurred_at": row.occurred_at.isoformat(),
                }
                for row in rows
            ]
