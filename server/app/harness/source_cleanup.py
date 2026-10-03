"""Release an optional source without turning terminal failures into degradation."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from .budget import BudgetDenied


@asynccontextmanager
async def close_after_source(close: Callable[[], Awaitable[None]]) -> AsyncIterator[None]:
    terminal = False
    try:
        yield
    except (BudgetDenied, asyncio.CancelledError):
        terminal = True
        raise
    finally:
        try:
            await close()
        except Exception:
            if not terminal:
                raise
