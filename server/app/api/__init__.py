"""HTTP API package."""
from .admin_avatar import create_admin_avatar_router
from .admin_config import create_admin_config_router
from .admin_dashboard import create_admin_dashboard_router
from .admin_jobs import create_admin_jobs_router
from .admin_logs_stream import create_logs_stream_router
from .admin_memory import create_admin_memory_router, create_deletion_ledger_router
from .admin_persona import create_admin_persona_router
from .admin_screen_awareness import create_admin_screen_awareness_router
from .admin_security import create_admin_security_router
from .admin_timeline import create_admin_timeline_router
from .auth import ChatSessionGuard, create_auth_router
from .avatar import create_avatar_router
from .briefs import create_briefs_router
from .calendar import create_calendar_router
from .chat import create_chat_router
from .chat_ws import ChatWebSocketManager, create_chat_websocket_router
from .cognition import create_cognition_router
from .device_commands import (
    DeviceCommandGateway,
    create_device_command_routers,
    sign_device_frame,
    verify_device_signature,
)
from .devices import DeviceCredentialGuard, create_device_routers
from .model_capabilities import create_model_capability_router
from .reviews import create_reviews_router
from .tasks import create_tasks_router
from .theme import create_admin_theme_router, create_theme_router
from .todo import create_todo_router
from .voice_ws import VoiceWebSocketManager, create_voice_websocket_router

__all__ = [
    "ChatSessionGuard",
    "ChatWebSocketManager",
    "DeviceCommandGateway",
    "DeviceCredentialGuard",
    "VoiceWebSocketManager",
    "create_admin_avatar_router",
    "create_admin_config_router",
    "create_admin_dashboard_router",
    "create_admin_jobs_router",
    "create_admin_memory_router",
    "create_admin_persona_router",
    "create_admin_screen_awareness_router",
    "create_admin_security_router",
    "create_admin_theme_router",
    "create_admin_timeline_router",
    "create_auth_router",
    "create_avatar_router",
    "create_briefs_router",
    "create_calendar_router",
    "create_chat_router",
    "create_chat_websocket_router",
    "create_cognition_router",
    "create_deletion_ledger_router",
    "create_device_command_routers",
    "create_device_routers",
    "create_logs_stream_router",
    "create_model_capability_router",
    "create_reviews_router",
    "create_tasks_router",
    "create_theme_router",
    "create_todo_router",
    "create_voice_websocket_router",
    "sign_device_frame",
    "verify_device_signature",
]
