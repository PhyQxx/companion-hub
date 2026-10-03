"""Weather provider adapter; terminal budget failures cross the optional-source boundary."""

from app.harness.budget import BudgetDenied
from app.harness.source_cleanup import close_after_source
from app.tools.factory import QueryToolRuntime
from app.tools.location import resolve_location

from .brief_models import BriefWeather


async def read_brief_weather(runtime: QueryToolRuntime, *, city: str) -> BriefWeather | None:
    async with close_after_source(runtime.close):
        try:
            resolved = await resolve_location(
                runtime.provider, explicit=city, ephemeral=None, default_city=city
            )
            live = await runtime.provider.weather(resolved.adcode, extensions="base")
            lives = live.get("lives")
            if not isinstance(lives, list) or not lives:
                return None
            item = lives[0] if isinstance(lives[0], dict) else {}
            forecast = await runtime.provider.weather(resolved.adcode, extensions="all")
            forecasts = forecast.get("forecasts")
            casts = (
                forecasts[0].get("casts")
                if isinstance(forecasts, list) and forecasts and isinstance(forecasts[0], dict)
                else None
            )
            today = (
                casts[0] if isinstance(casts, list) and casts and isinstance(casts[0], dict) else {}
            )
            return BriefWeather(
                city=resolved.name or city,
                condition=str(item.get("weather") or "未知"),
                temperature_c=str(item.get("temperature") or "—"),
                low_c=str(today.get("nighttemp")) if today.get("nighttemp") else None,
                high_c=str(today.get("daytemp")) if today.get("daytemp") else None,
            )
        except BudgetDenied:
            raise
        except Exception:
            return None
