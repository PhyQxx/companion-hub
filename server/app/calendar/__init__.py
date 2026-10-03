"""Public exports resolved without importing unrelated adapters."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .caldav import CalDavClient, CalDavError, CalDavSyncService
    from .caldav_scheduler import CalDavSyncScheduler
    from .google import (
        GoogleCalendarClient,
        GoogleCalendarError,
        GoogleCalendarSyncService,
        GoogleTokenStore,
    )
    from .google_scheduler import GoogleCalendarSyncScheduler
    from .models import CalendarEventView, CalendarParticipant, CalendarPreview
    from .service import CalendarService
    from .store import CalendarStore
    from .tools import CalendarCreateTool, CalendarSyncTool

_EXPORTS = {
    "CalDavClient": ("app.calendar.caldav", "CalDavClient"),
    "CalDavError": ("app.calendar.caldav", "CalDavError"),
    "CalDavSyncService": ("app.calendar.caldav", "CalDavSyncService"),
    "CalDavSyncScheduler": ("app.calendar.caldav_scheduler", "CalDavSyncScheduler"),
    "GoogleCalendarClient": ("app.calendar.google", "GoogleCalendarClient"),
    "GoogleCalendarError": ("app.calendar.google", "GoogleCalendarError"),
    "GoogleCalendarSyncService": ("app.calendar.google", "GoogleCalendarSyncService"),
    "GoogleTokenStore": ("app.calendar.google", "GoogleTokenStore"),
    "GoogleCalendarSyncScheduler": ("app.calendar.google_scheduler", "GoogleCalendarSyncScheduler"),
    "CalendarEventView": ("app.calendar.models", "CalendarEventView"),
    "CalendarParticipant": ("app.calendar.models", "CalendarParticipant"),
    "CalendarPreview": ("app.calendar.models", "CalendarPreview"),
    "CalendarService": ("app.calendar.service", "CalendarService"),
    "CalendarStore": ("app.calendar.store", "CalendarStore"),
    "CalendarCreateTool": ("app.calendar.tools", "CalendarCreateTool"),
    "CalendarSyncTool": ("app.calendar.tools", "CalendarSyncTool"),
}


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "CalDavClient",
    "CalDavError",
    "CalDavSyncScheduler",
    "CalDavSyncService",
    "CalendarCreateTool",
    "CalendarEventView",
    "CalendarParticipant",
    "CalendarPreview",
    "CalendarService",
    "CalendarStore",
    "CalendarSyncTool",
    "GoogleCalendarClient",
    "GoogleCalendarError",
    "GoogleCalendarSyncScheduler",
    "GoogleCalendarSyncService",
    "GoogleTokenStore",
]
