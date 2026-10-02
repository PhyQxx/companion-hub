"""Global observation sources remain bound to the oldest registered account."""

from uuid import UUID

from sqlalchemy import select

from app.db import AppUserRecord, Database


async def observation_owner(database: Database) -> UUID | None:
    # Select the original account before checking activity: filtering the inner
    # query to active users would silently reassign a private global source.
    primary = (
        select(AppUserRecord.id)
        .order_by(AppUserRecord.created_at, AppUserRecord.id)
        .limit(1)
        .correlate(None)
        .scalar_subquery()
    )
    async with database.sessions() as session:
        owner = await session.scalar(
            select(AppUserRecord.id).where(
                AppUserRecord.id == primary, AppUserRecord.status == "active"
            )
        )
    return owner if isinstance(owner, UUID) else None
