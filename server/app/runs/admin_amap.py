"""Own a two-request connectivity probe without copying its payload into Run."""

from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any
from uuid import UUID

from app.config import DatabaseConfigStore
from app.config.models import VoiceCostConfig
from app.db import Database
from app.harness.budget import BudgetDenied
from app.harness.joined_read import join_on_cancel
from app.harness.source_cleanup import close_after_source
from app.tools.amap_models import AmapProviderError
from app.tools.amap_probe_ports import AmapProbeClient, AmapProbeFailed, AmapProbeStep

from .admin_operation import admin_tool_request, owned_admin_sdk_request


async def probe_admin_amap(
    database: Database,
    store: DatabaseConfigStore,
    *,
    price: VoiceCostConfig | None,
    endpoint: str,
    client_factory: Callable[[], AmapProbeClient],
    source_guard: Callable[[], Awaitable[None]],
) -> tuple[AmapProbeStep, ...]:
    async def invoke(
        owner: UUID, begin: Callable[[], Awaitable[None]]
    ) -> tuple[AmapProbeStep, ...]:
        client: AmapProbeClient | None = None
        steps: list[AmapProbeStep] = []

        async def close() -> None:
            if client is not None:
                await join_on_cancel(client.close(), name="admin-amap-close")

        async def geocode() -> dict[str, Any]:
            nonlocal client
            client = client_factory()
            return await client.geocode("济南市")

        async def weather() -> dict[str, Any]:
            assert client is not None
            return await client.weather("370100", extensions="base")

        async def call(name: str, sdk: Callable[[], Awaitable[dict[str, Any]]]) -> None:
            started = perf_counter()
            sdk_error: Exception | None = None

            async def request() -> dict[str, Any]:
                nonlocal sdk_error
                try:
                    return await sdk()
                except BudgetDenied:
                    raise
                except Exception as error:
                    sdk_error = error
                    raise

            try:
                payload = await admin_tool_request(
                    owner=owner,
                    tool_name=f"admin.amap.{name}",
                    invoke=request,
                    source_guard=source_guard,
                    before_start=begin if name == "geocode" else None,
                )
            except Exception as error:
                # Only SDK errors become a probe step. Source/fee/permission
                # failures must propagate, never become a fallback request.
                if error is not sdk_error:
                    raise
                reason = (
                    error.reason_code
                    if isinstance(error, AmapProviderError)
                    else type(error).__name__
                )
                steps.append(
                    AmapProbeStep(
                        name, False, (perf_counter() - started) * 1000, reason, type(error).__name__
                    )
                )
                return
            valid = False
            if name == "geocode":
                adcode = payload.get("adcode")
                valid = isinstance(adcode, str) and len(adcode) == 6 and adcode.isdecimal()
                message = f"地理编码正常，返回 adcode={adcode}" if valid else "地理编码返回数据异常"
            else:
                lives = payload.get("lives")
                city = (
                    lives[0].get("city")
                    if isinstance(lives, list) and lives and isinstance(lives[0], dict)
                    else None
                )
                valid = isinstance(city, str) and bool(city.strip())
                message = f"天气接口正常，返回城市={city}" if valid else "天气接口返回数据异常"
            steps.append(AmapProbeStep(name, valid, (perf_counter() - started) * 1000, message))

        async with close_after_source(close):
            await call("geocode", geocode)
            if client is not None:
                await call("weather", weather)
            else:
                steps.append(AmapProbeStep("weather", False, 0, "SDK 未就绪，未发送天气请求"))
            result = tuple(steps)
            if not all(step.ok for step in result):
                raise AmapProbeFailed(result)
            return result

    return await owned_admin_sdk_request(
        database,
        store,
        entry="admin.amap.connection",
        price=price,
        endpoint=endpoint,
        invoke=invoke,
        source_guard=source_guard,
    )
