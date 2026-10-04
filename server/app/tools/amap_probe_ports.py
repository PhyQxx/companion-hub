"""Read-only provider probe contract independent of HTTP and persistence."""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class AmapProbeStep:
    name: str
    ok: bool
    latency_ms: float
    message: str
    error_type: str | None = None


class AmapProbeFailed(RuntimeError):
    def __init__(self, steps: tuple[AmapProbeStep, ...]) -> None:
        self.steps = steps
        super().__init__("amap_probe_failed")


class AmapProbeClient(Protocol):
    async def geocode(self, address: str) -> dict[str, Any]: ...

    async def weather(self, adcode: str, *, extensions: str) -> dict[str, Any]: ...

    async def close(self) -> None: ...
