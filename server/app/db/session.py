from __future__ import annotations

from collections.abc import Callable
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


DatabaseObserver = Callable[[Database], None]
_database_observer: DatabaseObserver | None = None


def set_database_observer(observer: DatabaseObserver | None) -> None:
    """Install a lifecycle observer used by the test harness to dispose every engine."""
    global _database_observer
    _database_observer = observer


def create_database(url: str, *, echo: bool = False) -> Database:
    engine = create_async_engine(url, echo=echo, pool_pre_ping=True)
    database = Database(
        engine=engine,
        sessions=async_sessionmaker(engine, expire_on_commit=False, autoflush=False),
    )
    if _database_observer is not None:
        _database_observer(database)
    return database
