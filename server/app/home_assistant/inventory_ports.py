"""Small read-only inventory port; no SDK, authentication or persistence."""

from dataclasses import dataclass
from typing import Protocol

from .models import HomeAssistantState


@dataclass(frozen=True)
class HomeAssistantInventory:
    states: tuple[HomeAssistantState, ...]
    areas: dict[str, str]
    devices: dict[str, dict[str, str | None]]


class HomeAssistantInventoryClient(Protocol):
    async def fetch_states(self) -> tuple[HomeAssistantState, ...]: ...

    async def fetch_entity_areas(self) -> dict[str, str]: ...

    async def fetch_entity_devices(self) -> dict[str, dict[str, str | None]]: ...

    async def close(self) -> None: ...
