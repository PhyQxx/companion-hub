"""Own an admin inventory workflow and count each admitted SDK request."""

import logging
from collections.abc import Awaitable, Callable
from typing import Literal
from uuid import UUID

from app.config import DatabaseConfigStore
from app.config.models import VoiceCostConfig
from app.db import Database
from app.harness.joined_read import join_on_cancel
from app.harness.source_cleanup import close_after_source
from app.home_assistant.inventory_ports import HomeAssistantInventory, HomeAssistantInventoryClient
from app.home_assistant.models import HomeAssistantError, HomeAssistantState

from .admin_operation import admin_tool_request, owned_admin_sdk_request

logger = logging.getLogger(__name__)


async def read_admin_inventory(
    database: Database,
    store: DatabaseConfigStore,
    *,
    operation: Literal["connection", "inventory"],
    price: VoiceCostConfig | None,
    endpoint: str,
    client_factory: Callable[[], HomeAssistantInventoryClient],
    source_guard: Callable[[], Awaitable[None]],
) -> HomeAssistantInventory:
    async def invoke(owner: UUID, begin: Callable[[], Awaitable[None]]) -> HomeAssistantInventory:
        client: HomeAssistantInventoryClient | None = None

        async def states() -> tuple[HomeAssistantState, ...]:
            nonlocal client
            await source_guard()
            client = client_factory()
            return await client.fetch_states()

        async def close() -> None:
            if client is not None:
                await join_on_cancel(client.close(), name="admin-ha-close")

        async with close_after_source(close):
            rows = await admin_tool_request(
                owner=owner,
                tool_name="admin.home_assistant.states",
                invoke=states,
                source_guard=source_guard,
                before_start=begin,
            )
            assert client is not None
            areas: dict[str, str] = {}
            devices: dict[str, dict[str, str | None]] = {}
            if operation == "inventory":
                try:
                    areas = await admin_tool_request(
                        owner=owner,
                        tool_name="admin.home_assistant.areas",
                        invoke=client.fetch_entity_areas,
                        source_guard=source_guard,
                    )
                except HomeAssistantError as error:
                    logger.warning(
                        "home assistant entity areas fetch failed: %s", error.reason_code
                    )
                try:
                    devices = await admin_tool_request(
                        owner=owner,
                        tool_name="admin.home_assistant.devices",
                        invoke=client.fetch_entity_devices,
                        source_guard=source_guard,
                    )
                except HomeAssistantError as error:
                    logger.warning(
                        "home assistant entity devices fetch failed: %s", error.reason_code
                    )
            return HomeAssistantInventory(rows, areas, devices)

    return await owned_admin_sdk_request(
        database,
        store,
        entry=f"admin.home_assistant.{operation}",
        price=price,
        endpoint=endpoint,
        invoke=invoke,
        source_guard=source_guard,
    )
