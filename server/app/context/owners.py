"""Global observation sources retain their first resolved account binding."""

from uuid import UUID

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError

from app.db import AppUserRecord, Database, ObservationOwnerBindingRecord


async def default_owner_id(database: Database) -> UUID | None:
    """多用户缺省目标：活跃业主（家庭级触发与集成的归属账户）。

    业主优先；无业主标识的库（旧备份恢复/测试种子等边缘态）回落最早
    活跃用户，保持单用户部署行为不变。多用户下家庭级后台任务（HA
    主动、安全巡检、主动投递兜底、日历/待办镜像）统一定向业主，
    不再随机落到最早创建的成员。
    """
    async with database.sessions() as session:
        value = await session.scalar(
            select(AppUserRecord.id)
            .where(AppUserRecord.status == "active")
            .order_by(
                case((AppUserRecord.role == "owner", 0), else_=1),
                AppUserRecord.created_at,
                AppUserRecord.id,
            )
            .limit(1)
        )
    return value if isinstance(value, UUID) else None


async def _binding(database: Database) -> tuple[bool, UUID | None]:
    async with database.sessions.begin() as session:
        row = (
            await session.execute(
                select(ObservationOwnerBindingRecord.user_id, AppUserRecord.status)
                .outerjoin(AppUserRecord, AppUserRecord.id == ObservationOwnerBindingRecord.user_id)
                .where(ObservationOwnerBindingRecord.slot == 1)
            )
        ).one_or_none()
    if row is None:
        return False, None
    owner = row.user_id
    return True, owner if isinstance(owner, UUID) and row.status == "active" else None


async def observation_owner(database: Database) -> UUID | None:
    bound, owner = await _binding(database)
    if bound:
        return owner
    # Finish the read before opening a write transaction. An empty installation
    # remains read-only; disabled originals still bind before activity checks.
    async with database.sessions.begin() as session:
        # 多用户：观测源与隐式主动输出绑定业主优先；无业主标识的库
        # （旧备份/测试种子）回落最早用户，保持单用户行为
        candidate = await session.scalar(
            select(AppUserRecord.id)
            .order_by(
                case((AppUserRecord.role == "owner", 0), else_=1),
                AppUserRecord.created_at,
                AppUserRecord.id,
            )
            .limit(1)
        )
    if not isinstance(candidate, UUID):
        return None
    inserting = False
    try:
        async with database.sessions.begin() as session:
            locked = await session.scalar(
                update(AppUserRecord)
                .where(AppUserRecord.id == candidate)
                .values(status=AppUserRecord.status)
                .returning(AppUserRecord.id)
            )
            if locked is None:
                return None
            existing = await session.get(ObservationOwnerBindingRecord, 1)
            if existing is None:
                session.add(ObservationOwnerBindingRecord(slot=1, user_id=candidate))
                inserting = True
                await session.flush()
    except IntegrityError:
        # Two first resolvers may have selected different accounts during an
        # account insertion. Only an established singleton resolves the race;
        # unrelated failures without a binding must remain visible.
        if not inserting:
            raise
        bound, owner = await _binding(database)
        if not bound:
            raise
        return owner
    _, owner = await _binding(database)
    return owner
