"""Global observation sources retain their first resolved account binding."""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db import AppUserRecord, Database, ObservationOwnerBindingRecord


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
        candidate = await session.scalar(
            select(AppUserRecord.id).order_by(AppUserRecord.created_at, AppUserRecord.id).limit(1)
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
