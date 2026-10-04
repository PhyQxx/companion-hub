"""Admission port for each physical calendar HTTP request."""

from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

T = TypeVar("T")


class CalendarRequestRunner(Protocol):
    async def __call__(self, method: str, invoke: Callable[[], Awaitable[T]]) -> T: ...
