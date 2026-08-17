from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@dataclass(frozen=True, slots=True)
class Database:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    async def close(self) -> None:
        await self.engine.dispose()


def create_database(url: str, *, echo: bool = False) -> Database:
    engine = create_async_engine(url, echo=echo, pool_pre_ping=True)
    return Database(
        engine=engine,
        sessions=async_sessionmaker(engine, expire_on_commit=False, autoflush=False),
    )
