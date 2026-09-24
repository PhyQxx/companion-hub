from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.adapters import AdapterRegistry
from app.adapters.builtin import create_builtin_registry
from app.api import (
    create_admin_avatar_router,
    create_admin_backups_router,
    create_admin_browser_awareness_router,
    create_admin_butler_router,
    create_admin_config_router,
    create_admin_dashboard_router,
    create_admin_export_router,
    create_admin_jobs_router,
    create_admin_mcp_router,
    create_admin_memory_router,
    create_admin_persona_router,
    create_admin_safety_router,
    create_admin_screen_awareness_router,
    create_admin_security_router,
    create_admin_senseaudio_router,
    create_admin_tasks_router,
    create_admin_theme_router,
    create_admin_timeline_router,
    create_admin_voice_router,
    create_auth_router,
    create_avatar_router,
    create_briefs_router,
    create_calendar_router,
    create_chat_router,
    create_chat_websocket_router,
    create_cognition_router,
    create_contacts_router,
    create_deletion_ledger_router,
    create_device_command_routers,
    create_device_routers,
    create_home_scenes_router,
    create_logs_stream_router,
    create_meetings_router,
    create_model_capability_router,
    create_pnkx_router,
    create_push_router,
    create_reviews_router,
    create_safety_router,
    create_tasks_router,
    create_theme_router,
    create_todo_router,
    create_voice_websocket_router,
    create_workflows_router,
    create_xiaoai_websocket_router,
)
from app.api.admin_config import set_runtime_admin_token
from app.api.events import create_event_router
from app.api.mail import create_mail_router
from app.appearance import ThemeStore
from app.auth import AuthService
from app.avatar import AvatarAssetImporter, AvatarStore
from app.browser_awareness import (
    BrowserAwarenessAnalyzer,
    BrowserAwarenessGateway,
    BrowserAwarenessLoop,
    BrowserAwarenessResolver,
    BrowserAwarenessTabHints,
    LlmBrowserAnalyzer,
)
from app.bus import DispatcherWorker, EventPublisher, LocalEventPublisher
from app.calendar import (
    CalDavSyncScheduler,
    CalDavSyncService,
    CalendarCreateTool,
    CalendarService,
    CalendarStore,
    CalendarSyncTool,
    GoogleCalendarSyncScheduler,
    GoogleCalendarSyncService,
)
from app.chat import ChatService, CompositeRuntimeCapabilityProvider, RuntimeCapabilityProvider
from app.cognition import (
    ActionPlanService,
    AttentionEngine,
    CognitiveCycle,
    CognitiveStore,
    GoalTracker,
    PlanExecutionEvent,
    RouterDeliberator,
    RuleBasedDeliberator,
    SemanticEvent,
    ToolActionRunner,
    WorldStateBuilder,
    build_builtin_action_registry,
)
from app.cognition.action_plan import PlanChangedEvent
from app.commute import CommuteCheckTool, CommuteService
from app.config import (
    BrowserWorkflowConfig,
    ConfigStore,
    ConfigWatcher,
    DatabaseConfigStore,
    DesktopActionsConfig,
)
from app.confirmation import DatabasePendingMutationStore
from app.contacts import ContactQueryTool, ContactSaveTool, ContactStore
from app.db import Database, create_database
from app.devices import (
    DeviceCommandStore,
    DeviceRegistry,
    DeviceTargetResolver,
    MqttPresenceBridge,
)
from app.devices.mqtt_client import MqttDeviceClient, MqttTelemetryBuffer
from app.focus import FocusScheduler, FocusService, FocusStartTool, FocusStatusTool, FocusStopTool
from app.home_assistant import (
    HomeAssistantManager,
    HomeAssistantProactiveEngine,
    HomeControlTool,
    HomeGetHistoryTool,
    HomeGetStateTool,
    SearchDevicesTool,
)
from app.home_scene import (
    HomeSceneListTool,
    HomeSceneRunTool,
    HomeSceneService,
    HomeSceneStore,
)
from app.integrations.mcp import McpManager
from app.integrations.mcp.actions import sync_mcp_actions
from app.integrations.mcp.chat_tools import McpChatToolProvider
from app.jobs import AssetStore, JobEngine
from app.llm.provider import EnvSecretProvider
from app.mail import MailAttachmentStore, MailClient, MailSendTool, create_mail_tools
from app.meetings import LlmMeetingSummarizer, MeetingService, MeetingStore
from app.memory import (
    LlmMemoryExtractor,
    MemoryExtractor,
    MemoryIngester,
    MemoryRetriever,
    MemoryStore,
)
from app.model_capabilities import CapabilityModelService
from app.observability import apply_observability, configure_logging
from app.output import ProactiveDeliveryService
from app.output.proactive import DesktopCommandGateway
from app.perception import PerceptionPipeline, PerceptionStore, ProactivePolicy
from app.perception.pipeline import EventObserver
from app.persona import PersonaStore
from app.pnkx import PnkxCreateTool, PnkxLifeClient, PnkxReadTool, pnkx_runs_local
from app.push import PushSubscriptionStore, WebPushAdapter
from app.runtime import TurnCoordinator
from app.safety import ActivityTracker, SafetyActivityScheduler, SafetyAlertService
from app.schemas.common import PrivacyLevel
from app.screen_awareness import (
    ScreenAwarenessAnalyzer,
    ScreenAwarenessGateway,
    ScreenAwarenessLoop,
    ScreenAwarenessResolver,
)
from app.tasks import ReminderCreateTool, TaskScheduler, TaskStore
from app.tasks.brief import BriefCommute, BriefWeather, DailyBriefService
from app.tasks.brief_scheduler import DailyBriefScheduler
from app.tasks.goal_scheduler import GoalReminderScheduler
from app.tasks.review import DailyReviewService
from app.tasks.review_scheduler import DailyReviewScheduler
from app.timeline import HistoryRecallService, TimelineStore
from app.todo import PnkxTodoClient, TodoSyncService
from app.todo.sync_scheduler import TodoSyncScheduler
from app.tools import (
    DesktopNotifyTool,
    ToolExecutor,
    ToolHandler,
    ToolRegistry,
    build_query_tool_runtime,
)
from app.tools.browser import InspectWebpageTool
from app.tools.browser_form import (
    BrowserFormFillTool,
    BrowserFormReadTool,
    BrowserFormSubmitTool,
    BrowserOpenTabTool,
)
from app.tools.desktop_actions import (
    DesktopClipboardWriteTool,
    DesktopOpenAppTool,
    DesktopOpenUrlTool,
    DesktopSetVolumeTool,
)
from app.tools.location import resolve_location
from app.tools.mcp_actions import McpToolCallTool
from app.tools.screen import CapabilityScreenAnalyzer, CaptureScreenTool
from app.tools.sensors import ReadSensorsTool
from app.voice import ConfigVoiceSource
from app.workflows import (
    WorkflowRunTool,
    WorkflowSaveTool,
    WorkflowService,
    WorkflowStore,
)
from app.xiaoai_config import XiaoAiConfigMaterializer


def _parse_brief_time(raw: str) -> dt_time:
    hour, minute = raw.split(":", 1)
    return dt_time(int(hour), int(minute))


def _parse_review_time(raw: str) -> dt_time:
    hour, minute = raw.split(":", 1)
    return dt_time(int(hour), int(minute))


def create_app(
    database: Database | None = None,
    *,
    enable_dev_endpoints: bool | None = None,
    run_dispatcher: bool | None = None,
    event_publisher: EventPublisher | None = None,
    adapter_registry: AdapterRegistry | None = None,
    config_store: ConfigStore | DatabaseConfigStore | None = None,
    watch_config: bool | None = None,
    admin_token: str | None = None,
) -> FastAPI:
    import asyncio

    broadcast_handler = configure_logging(os.getenv("ARIA_LOG_LEVEL", "INFO"))
    if broadcast_handler is not None:
        with suppress(RuntimeError):
            broadcast_handler.set_event_loop(asyncio.get_running_loop())
    database_url = os.getenv("ARIA_DATABASE_URL")
    runtime_database = database or (create_database(database_url) if database_url else None)
    owns_database = database is None and runtime_database is not None
    runtime_adapters = adapter_registry or create_builtin_registry()
    config_path = os.getenv("ARIA_CONFIG_PATH")
    if config_store is not None:
        runtime_config: ConfigStore | DatabaseConfigStore | None = config_store
    elif config_path and runtime_database is not None:
        runtime_config = DatabaseConfigStore(runtime_database, Path(config_path))
    elif config_path:
        runtime_config = ConfigStore(Path(config_path))
    else:
        runtime_config = None
    config_watch_enabled = watch_config
    if config_watch_enabled is None:
        config_watch_enabled = os.getenv("ARIA_WATCH_CONFIG", "true").lower() == "true"
    config_watcher = (
        ConfigWatcher(
            runtime_config,
            on_reload=lambda snapshot: apply_observability(snapshot.config),
        )
        if isinstance(runtime_config, ConfigStore) and config_watch_enabled
        else None
    )
    capability_models = (
        CapabilityModelService(runtime_config) if runtime_config is not None else None
    )
    home_assistant_manager = (
        HomeAssistantManager(runtime_config) if runtime_config is not None else None
    )
    xiaoai_config_path = os.getenv("ARIA_XIAOAI_CONFIG_PATH")
    xiaoai_materializer = (
        XiaoAiConfigMaterializer(runtime_config, Path(xiaoai_config_path))
        if runtime_config is not None and xiaoai_config_path
        else None
    )
    dispatcher_enabled = run_dispatcher
    if dispatcher_enabled is None:
        dispatcher_enabled = os.getenv("ARIA_RUN_DISPATCHER", "false").lower() == "true"
    worker = None
    runtime_chat_service: ChatService | None = None
    home_assistant_proactive: HomeAssistantProactiveEngine | None = None
    screen_awareness_loop: ScreenAwarenessLoop | None = None
    browser_awareness_loop: BrowserAwarenessLoop | None = None
    mcp_manager = McpManager(runtime_config) if runtime_config is not None else None
    safety_alert_service: SafetyAlertService | None = None
    activity_tracker = ActivityTracker()
    activity_scheduler: SafetyActivityScheduler | None = None
    proactive_delivery: ProactiveDeliveryService | None = None
    mqtt_presence_bridge: MqttPresenceBridge | None = None
    task_scheduler: TaskScheduler | None = None
    goal_reminder_scheduler: GoalReminderScheduler | None = None
    daily_brief_scheduler: DailyBriefScheduler | None = None
    daily_review_scheduler: DailyReviewScheduler | None = None
    todo_sync_scheduler: TodoSyncScheduler | None = None
    caldav_sync_service: CalDavSyncService | None = None
    caldav_sync_scheduler: CalDavSyncScheduler | None = None
    google_calendar_sync_service: GoogleCalendarSyncService | None = None
    google_calendar_sync_scheduler: GoogleCalendarSyncScheduler | None = None

    async def test_home_assistant_proactive() -> bool:
        if home_assistant_proactive is None:
            return False
        return await home_assistant_proactive.send_test_message() is not None

    persona_store = PersonaStore(runtime_database) if runtime_database is not None else None
    memory_store = MemoryStore(runtime_database) if runtime_database is not None else None
    timeline_store = TimelineStore(runtime_database) if runtime_database is not None else None
    device_registry = DeviceRegistry(runtime_database) if runtime_database is not None else None
    device_command_store = (
        DeviceCommandStore(runtime_database) if runtime_database is not None else None
    )
    job_engine = JobEngine(runtime_database) if runtime_database is not None else None
    asset_store = (
        AssetStore(runtime_database, Path(os.getenv("ARIA_ASSET_DIR", "./assets")))
        if runtime_database is not None
        else None
    )
    avatar_store = AvatarStore(runtime_database) if runtime_database is not None else None
    avatar_upload_root = Path(
        os.getenv("ARIA_AVATAR_ASSET_DIR", "./assets/avatar-uploads")
    ).resolve()
    avatar_upload_root.mkdir(parents=True, exist_ok=True)
    avatar_importer = (
        AvatarAssetImporter(avatar_upload_root, avatar_store) if avatar_store is not None else None
    )
    theme_store = (
        ThemeStore(
            runtime_database,
            timezone_name=os.getenv("ARIA_DEFAULT_TIMEZONE", "Asia/Shanghai"),
        )
        if runtime_database is not None
        else None
    )
    cognitive_store = CognitiveStore(runtime_database) if runtime_database is not None else None
    action_registry = build_builtin_action_registry()
    action_plan_service = (
        ActionPlanService(runtime_database, action_registry)
        if runtime_database is not None
        else None
    )
    # FLOW-01：惰性引用计划服务，运行时展开计划即可拿到最终注入的 runner。
    workflow_service = (
        WorkflowService(
            WorkflowStore(runtime_database),
            action_registry,
            lambda: action_plan_service,  # type: ignore[arg-type,return-value]
        )
        if runtime_database is not None
        else None
    )
    cognitive_cycle = (
        CognitiveCycle(
            cognitive_store,
            WorldStateBuilder(
                runtime_database,
                cognitive_store,
                memory_retriever=MemoryRetriever(memory_store) if memory_store else None,
                timeline_store=timeline_store,
            ),
            AttentionEngine(),
            (
                RouterDeliberator(runtime_config)
                if runtime_config is not None
                else RuleBasedDeliberator()
            ),
        )
        if runtime_database is not None and cognitive_store is not None
        else None
    )
    perception_store = PerceptionStore(runtime_database) if runtime_database is not None else None
    perception_pipeline = (
        PerceptionPipeline(
            cognitive_cycle,
            perception_store,
            ProactivePolicy(runtime_database),
        )
        if runtime_database is not None
        and cognitive_cycle is not None
        and perception_store is not None
        else None
    )
    task_store = TaskStore(runtime_database) if runtime_database is not None else None
    if task_store is not None:
        task_scheduler = TaskScheduler(
            task_store,
            interval_seconds=float(os.getenv("ARIA_TASK_SCHEDULER_INTERVAL", "15")),
        )
        if perception_pipeline is not None:

            async def dispatch_semantic_event(event: SemanticEvent) -> None:
                try:
                    await task_scheduler.on_semantic_event(cast(Any, event))
                except Exception:
                    logging.getLogger(__name__).exception("task semantic observer failed")
                if home_scene_service is not None:
                    try:
                        await home_scene_service.handle_semantic_event(
                            event.user_id, event.event_id, event.kind, cancel_arrivals=False
                        )
                    except Exception:
                        logging.getLogger(__name__).exception("home scene observer failed")

            perception_pipeline.set_event_observer(cast(EventObserver, dispatch_semantic_event))
    goal_tracker = GoalTracker(cognitive_store) if cognitive_store is not None else None
    goal_reminder_scheduler = (
        GoalReminderScheduler(
            cognitive_store,
            interval_seconds=float(os.getenv("ARIA_GOAL_REMINDER_INTERVAL", "60")),
        )
        if cognitive_store is not None
        else None
    )
    focus_service = (
        FocusService(
            timeline_store,
            long_work_minutes=int(os.getenv("ARIA_FOCUS_LONG_WORK_MINUTES", "90")),
        )
        if timeline_store is not None
        else None
    )
    focus_scheduler = (
        FocusScheduler(
            focus_service,
            interval_seconds=float(os.getenv("ARIA_FOCUS_INTERVAL", "120")),
        )
        if focus_service is not None
        else None
    )
    # HOME-01：场景服务惰性引用计划服务；观察口在 pipeline 就绪处组合分发
    home_scene_service = (
        HomeSceneService(
            HomeSceneStore(runtime_database),
            lambda: action_plan_service,  # type: ignore[arg-type,return-value]
            build_builtin_action_registry(),
        )
        if runtime_database is not None
        else None
    )

    if perception_pipeline is not None and home_scene_service is not None:

        async def cancel_home_on_departure(event: SemanticEvent) -> None:
            await home_scene_service.on_departure(event.user_id, event.occurred_at)

        perception_pipeline.set_departure_observer(cancel_home_on_departure)

    async def fetch_brief_weather() -> BriefWeather | None:
        """按需构建高德运行时取一次天气；任何失败只意味着简报少一条事实。"""
        if runtime_config is None:
            return None
        city = runtime_config.current.config.tools.query.default_city or os.getenv(
            "ARIA_BRIEF_CITY"
        )
        if not city:
            return None
        try:
            runtime = build_query_tool_runtime(runtime_config.current.config, EnvSecretProvider())
        except ValueError:
            return None
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
        except Exception:
            return None
        finally:
            await runtime.close()

    contact_store = ContactStore(runtime_database) if runtime_database is not None else None

    def build_commute_service() -> CommuteService | None:
        """按需构建出行服务（含高德 provider，调用方用完即关）。"""
        if runtime_config is None or runtime_database is None or task_store is None:
            return None
        if calendar_service is None:
            return None
        commute_config = runtime_config.current.config.tools.commute
        if not commute_config.enabled or commute_config.origin is None:
            return None
        try:
            runtime = build_query_tool_runtime(runtime_config.current.config, EnvSecretProvider())
        except ValueError:
            return None
        return CommuteService(
            calendar_service.store,
            task_store,
            runtime.provider,
            origin=commute_config.origin,
            mode=commute_config.mode,
            buffer_minutes=commute_config.buffer_minutes,
            default_city=runtime_config.current.config.tools.query.default_city,
            clock=lambda: datetime.now(UTC),
        )

    calendar_store = CalendarStore(runtime_database) if runtime_database is not None else None

    async def fetch_brief_commute(user_id: UUID) -> BriefCommute | None:
        """当日首个带地点日程的出行建议；只读计算（不建提醒），失败静默降级。"""
        service = build_commute_service()
        if service is None:
            return None
        brief_tz = ZoneInfo(os.getenv("ARIA_DEFAULT_TIMEZONE", "Asia/Shanghai"))
        try:
            event = await service.next_outing(user_id, within_hours=24)
            if event is None:
                return None
            if event.starts_at.astimezone(brief_tz).date() != datetime.now(brief_tz).date():
                return None  # 今天没有带地点的日程，不为明天建议（简报无日期字段）
            plan = await service.plan_commute(user_id, event, reminder=False)
            return BriefCommute(
                destination=plan.destination_text,
                leave_by=plan.leave_by,
                event_title=plan.event_title,
                starts_at=plan.starts_at,
                mode=plan.mode,
                duration_min=int(plan.duration_s // 60) if plan.duration_s else None,
            )
        finally:
            await service.aclose()

    daily_brief_service = (
        DailyBriefService(
            runtime_database,
            task_store,
            cognitive_store,
            contact_store=contact_store,
            calendar_store=calendar_store,
            weather_fetcher=fetch_brief_weather,
            commute_fetcher=fetch_brief_commute,
            timezone_name=os.getenv("ARIA_DEFAULT_TIMEZONE", "Asia/Shanghai"),
        )
        if runtime_database is not None and task_store is not None and cognitive_store is not None
        else None
    )
    daily_brief_scheduler = (
        DailyBriefScheduler(
            daily_brief_service,
            timezone_name=os.getenv("ARIA_DEFAULT_TIMEZONE", "Asia/Shanghai"),
            brief_time=_parse_brief_time(os.getenv("ARIA_BRIEF_TIME", "08:00")),
        )
        if daily_brief_service is not None
        else None
    )
    daily_review_service = (
        DailyReviewService(
            runtime_database,
            task_store,
            cognitive_store,
            calendar_store=calendar_store,
            timezone_name=os.getenv("ARIA_DEFAULT_TIMEZONE", "Asia/Shanghai"),
        )
        if runtime_database is not None and task_store is not None and cognitive_store is not None
        else None
    )
    daily_review_scheduler = (
        DailyReviewScheduler(
            daily_review_service,
            timezone_name=os.getenv("ARIA_DEFAULT_TIMEZONE", "Asia/Shanghai"),
            review_time=_parse_review_time(os.getenv("ARIA_REVIEW_TIME", "21:30")),
        )
        if daily_review_service is not None
        else None
    )
    calendar_service = (
        CalendarService(calendar_store, task_store)
        if calendar_store is not None and task_store is not None
        else None
    )
    meeting_service = (
        MeetingService(
            MeetingStore(runtime_database),
            calendar_store,
            task_store,
            LlmMeetingSummarizer(runtime_config),
        )
        if runtime_database is not None
        and calendar_store is not None
        and task_store is not None
        and runtime_config is not None
        else None
    )

    # TODO-01：pnkx 为任务单一真源。请求时动态读取配置中心，环境变量仅作旧部署回退。
    def pnkx_settings() -> tuple[str, str, bool]:
        if runtime_config is not None:
            try:
                config = runtime_config.current.config.integrations.pnkx
            except RuntimeError:
                config = None
            if config is not None and config.enabled and config.base_url is not None:
                token = config.secret_value
                if token is None and config.secret_ref is not None:
                    token = EnvSecretProvider().resolve(config.secret_ref)
                if token:
                    return str(config.base_url).rstrip("/"), token, config.writes_enabled
        return (
            (os.getenv("ARIA_PNKX_BASE_URL") or "").rstrip("/"),
            os.getenv("ARIA_PNKX_TOKEN") or "",
            os.getenv("ARIA_PNKX_WRITES_ENABLED", "false").lower() == "true",
        )

    pnkx_base_url, pnkx_integration_token, _ = pnkx_settings()

    def pnkx_sync_interval() -> float:
        if runtime_config is not None:
            try:
                config = runtime_config.current.config.integrations.pnkx
            except RuntimeError:
                config = None
            if config is not None and config.enabled:
                return float(config.sync_interval_seconds)
        return float(os.getenv("ARIA_TODO_SYNC_INTERVAL", "300"))

    todo_sync_service = (
        TodoSyncService(
            runtime_database,
            PnkxTodoClient(
                base_url=pnkx_base_url or "",
                integration_token=pnkx_integration_token or "",
                settings_provider=pnkx_settings,
                timezone_name=os.getenv("ARIA_DEFAULT_TIMEZONE", "Asia/Shanghai"),
            ),
        )
        if runtime_database is not None
        and (runtime_config is not None or (pnkx_base_url and pnkx_integration_token))
        else None
    )
    pnkx_life_client = (
        PnkxLifeClient(
            base_url=pnkx_base_url or "",
            integration_token=pnkx_integration_token or "",
            settings_provider=pnkx_settings,
        )
        if runtime_config is not None or (pnkx_base_url and pnkx_integration_token)
        else None
    )
    todo_sync_scheduler = (
        TodoSyncScheduler(
            todo_sync_service,
            interval_provider=pnkx_sync_interval,
        )
        if todo_sync_service is not None
        else None
    )
    # CAL-01：CalDAV 外部日历只读镜像；配置中心 integrations.calendar.caldav 启用
    caldav_sync_service = (
        CalDavSyncService(runtime_database, runtime_config)
        if runtime_database is not None and runtime_config is not None
        else None
    )
    caldav_sync_scheduler = (
        CalDavSyncScheduler(
            caldav_sync_service,
            interval_seconds=float(os.getenv("ARIA_CALDAV_SYNC_INTERVAL", "900")),
        )
        if caldav_sync_service is not None
        else None
    )
    google_calendar_sync_service = (
        GoogleCalendarSyncService(runtime_database, runtime_config)
        if runtime_database is not None and runtime_config is not None
        else None
    )
    google_calendar_sync_scheduler = (
        GoogleCalendarSyncScheduler(
            google_calendar_sync_service,
            interval_seconds=float(os.getenv("ARIA_GOOGLE_SYNC_INTERVAL", "900")),
        )
        if google_calendar_sync_service is not None
        else None
    )

    async def deliver_task_reminder(
        text: str,
        *,
        user_id: UUID,
        task_id: UUID,
        privacy_level: PrivacyLevel,
        trigger_kind: str,
    ) -> list[str] | None:
        if proactive_delivery is None:
            return None
        result = await proactive_delivery.deliver(
            text,
            entity_id="task",
            rule_id=f"task:{task_id}",
            trigger_kind=trigger_kind,
            privacy_level=privacy_level,
            target_user_id=user_id,
        )
        if result is None:
            return None
        return list(result.delivered_channels)

    async def deliver_goal_reminder(
        text: str,
        *,
        user_id: UUID,
        goal_id: UUID,
        privacy_level: str,
        trigger_kind: str,
    ) -> list[str] | None:
        if proactive_delivery is None:
            return None
        result = await proactive_delivery.deliver(
            text,
            entity_id="goal",
            rule_id=f"goal:{goal_id}",
            trigger_kind=trigger_kind,
            privacy_level=PrivacyLevel(privacy_level),
            target_user_id=user_id,
        )
        if result is None:
            return None
        return list(result.delivered_channels)

    history_recall = (
        HistoryRecallService(
            timeline_store,
            timezone_name=os.getenv("ARIA_DEFAULT_TIMEZONE", "Asia/Shanghai"),
        )
        if timeline_store is not None
        else None
    )
    memory_extractor: MemoryExtractor | None = None
    # 默认 LLM 提取器(llm-utility-v1): utility 路由结构化提取, 规则提取器
    # 仅在模型故障时兜底; 需要纯离线确定性时显式设 ARIA_MEMORY_EXTRACTOR=rule
    if memory_store is not None and os.getenv("ARIA_MEMORY_EXTRACTOR", "llm") == "llm":
        memory_extractor = LlmMemoryExtractor()
    if dispatcher_enabled and runtime_database is not None:
        publisher = event_publisher or LocalEventPublisher(runtime_database)
        worker = DispatcherWorker(runtime_database.sessions, publisher)

    mqtt_client: MqttDeviceClient | None = None
    if os.getenv("ARIA_MQTT_ENABLED", "false").lower() == "true":
        mqtt_client = MqttDeviceClient(
            host=os.getenv("ARIA_MQTT_HOST", "localhost"),
            port=int(os.getenv("ARIA_MQTT_PORT", "1883")),
            username=os.getenv("ARIA_MQTT_USERNAME"),
            password=os.getenv("ARIA_MQTT_PASSWORD"),
            telemetry_buffer=MqttTelemetryBuffer(),
        )

    turn_coordinator: TurnCoordinator | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if runtime_config is not None:
            await runtime_config.load()
            apply_observability(runtime_config.current.config)
            if xiaoai_materializer is not None:
                await xiaoai_materializer.write()
        if persona_store is not None:
            await persona_store.load()
        if home_assistant_manager is not None:
            await home_assistant_manager.start()
        if mqtt_client is not None:
            await mqtt_client.start()
        if avatar_store is not None:
            await avatar_store.load_builtin_packs()
        if theme_store is not None:
            await theme_store.load_builtin_themes()
        if runtime_chat_service is not None:
            await runtime_chat_service.recover_incomplete_turns()
        if turn_coordinator is not None:
            # 重启后把不安全的未完成回合标记为 cancelled，并清理遗留音频/麦克风租约
            recovered_turns = await turn_coordinator.recover_after_restart()
            await turn_coordinator.expire_stale_leases()
            if recovered_turns:
                logging.getLogger(__name__).info(
                    "recovered %s unsafe turns after restart", recovered_turns
                )
        if config_watcher is not None:
            await config_watcher.start()
        if worker is not None:
            await worker.start()
        if screen_awareness_loop is not None:
            screen_awareness_loop.start()
        if browser_awareness_loop is not None:
            browser_awareness_loop.start()
        if mcp_manager is not None:
            mcp_manager.start()
        if safety_alert_service is not None:
            await safety_alert_service.resume()
        if activity_scheduler is not None:
            await activity_scheduler.start()
        if task_scheduler is not None:
            task_scheduler.start()
        if goal_reminder_scheduler is not None:
            goal_reminder_scheduler.start()
        if focus_scheduler is not None:
            focus_scheduler.start()
        if daily_brief_scheduler is not None:
            daily_brief_scheduler.start()
        if daily_review_scheduler is not None:
            daily_review_scheduler.start()
        if todo_sync_scheduler is not None:
            todo_sync_scheduler.start()
        if caldav_sync_scheduler is not None:
            caldav_sync_scheduler.start()
        if google_calendar_sync_scheduler is not None:
            google_calendar_sync_scheduler.start()
        try:
            yield
        finally:
            if pnkx_life_client is not None:
                await pnkx_life_client.close()
            if todo_sync_scheduler is not None:
                await todo_sync_scheduler.stop()
            if caldav_sync_scheduler is not None:
                await caldav_sync_scheduler.stop()
            if google_calendar_sync_scheduler is not None:
                await google_calendar_sync_scheduler.stop()
            if daily_review_scheduler is not None:
                await daily_review_scheduler.stop()
            if daily_brief_scheduler is not None:
                await daily_brief_scheduler.stop()
            if goal_reminder_scheduler is not None:
                await goal_reminder_scheduler.stop()
            if task_scheduler is not None:
                await task_scheduler.stop()
            if screen_awareness_loop is not None:
                await screen_awareness_loop.stop()
            if browser_awareness_loop is not None:
                await browser_awareness_loop.stop()
            if mcp_manager is not None:
                await mcp_manager.stop()
            if safety_alert_service is not None:
                await safety_alert_service.stop()
            if activity_scheduler is not None:
                await activity_scheduler.stop()
            if home_assistant_proactive is not None:
                await home_assistant_proactive.stop()
            if mqtt_client is not None:
                await mqtt_client.stop()
            if mqtt_presence_bridge is not None:
                await mqtt_presence_bridge.stop()
            if perception_pipeline is not None:
                await perception_pipeline.stop()
            if runtime_chat_service is not None:
                # 等待仍在执行的后台记忆沉淀收尾，避免丢最后一轮的事实
                await runtime_chat_service.drain_background_work()
            if worker is not None:
                await worker.stop()
            if home_assistant_manager is not None:
                await home_assistant_manager.stop()
            if config_watcher is not None:
                await config_watcher.stop()
            if owns_database and runtime_database is not None:
                await runtime_database.close()

    app = FastAPI(title="Aria Companion Hub", version=__version__, lifespan=lifespan)
    app.state.database = runtime_database
    app.state.dispatcher = worker
    app.state.adapter_registry = runtime_adapters
    app.state.config_store = runtime_config
    app.state.config_watcher = config_watcher
    app.state.persona_store = persona_store
    app.state.memory_store = memory_store
    app.state.timeline_store = timeline_store
    app.state.perception_pipeline = perception_pipeline
    app.state.perception_store = perception_store
    app.state.device_registry = device_registry
    app.state.device_command_store = device_command_store
    app.state.history_recall_service = history_recall
    app.state.capability_model_service = capability_models
    app.state.home_assistant_manager = home_assistant_manager
    app.state.job_engine = job_engine
    app.state.asset_store = asset_store
    app.state.avatar_store = avatar_store
    app.state.avatar_importer = avatar_importer
    app.state.theme_store = theme_store
    app.state.action_registry = action_registry
    app.state.action_plan_service = action_plan_service

    admin_root = Path(__file__).parent / "admin"
    app.mount("/admin/legacy", StaticFiles(directory=admin_root), name="admin-legacy")
    admin_dist = Path(__file__).resolve().parents[2] / "web" / "apps" / "admin" / "dist"
    admin_spa_ready = (admin_dist / "index.html").is_file()
    if admin_spa_ready:
        app.mount(
            "/admin/assets",
            StaticFiles(directory=admin_dist / "assets"),
            name="admin-assets",
        )
    chat_root = Path(__file__).parent / "chat_ui"
    app.mount("/chat/debug/assets", StaticFiles(directory=chat_root), name="chat-debug-assets")
    pet_ui_root = Path(__file__).parent / "pet_ui"
    app.mount(
        "/desktop/pet",
        StaticFiles(directory=pet_ui_root, html=True),
        name="desktop-pet",
    )
    chat_dist = Path(__file__).resolve().parents[2] / "web" / "apps" / "chat" / "dist"
    chat_spa_ready = (chat_dist / "index.html").is_file()
    chat_dist_assets = chat_dist / "assets"
    if chat_dist_assets.is_dir():
        app.mount("/chat/assets", StaticFiles(directory=chat_dist_assets), name="chat-assets")
    chat_dist_icons = chat_dist / "icons"
    if chat_dist_icons.is_dir():
        app.mount("/chat/icons", StaticFiles(directory=chat_dist_icons), name="chat-icons")
    avatar_assets_root = Path(__file__).parent / "avatar" / "assets"
    app.mount(
        "/api/v1/avatar-assets",
        StaticFiles(directory=avatar_assets_root),
        name="avatar-assets",
    )
    app.mount(
        "/api/v1/avatar-user-assets",
        StaticFiles(directory=avatar_upload_root),
        name="avatar-user-assets",
    )
    # Resolve this at app startup so a newly installed runtime is served after restart/reload.
    configured_live2d_runtime_dir = os.getenv("ARIA_LIVE2D_RUNTIME_DIR")
    default_live2d_runtime_dir = (
        Path.home()
        / "Library"
        / "Application Support"
        / "AriaCompanionHub"
        / "live2d-runtime"
        / "current"
    )
    live2d_runtime_dir = (
        Path(configured_live2d_runtime_dir).expanduser()
        if configured_live2d_runtime_dir
        else default_live2d_runtime_dir
    )
    if live2d_runtime_dir.is_dir():
        app.mount(
            "/api/v1/avatar-live2d-runtime",
            StaticFiles(directory=live2d_runtime_dir.resolve()),
            name="avatar-live2d-runtime",
        )

    if admin_spa_ready:

        @app.get("/admin", include_in_schema=False)
        @app.get("/admin/{rest:path}", include_in_schema=False)
        async def admin_spa(rest: str = "") -> FileResponse:
            return FileResponse(admin_dist / "index.html")

    else:

        @app.get("/admin/models", include_in_schema=False)
        async def model_admin() -> FileResponse:
            return FileResponse(admin_root / "models.html")

        @app.get("/admin", include_in_schema=False)
        @app.get("/admin/devices", include_in_schema=False)
        @app.get("/admin/logs", include_in_schema=False)
        @app.get("/admin/privacy", include_in_schema=False)
        @app.get("/admin/settings", include_in_schema=False)
        async def admin_module() -> FileResponse:
            return FileResponse(admin_root / "module.html")

        @app.get("/admin/personas", include_in_schema=False)
        async def persona_admin() -> FileResponse:
            return FileResponse(admin_root / "personas.html")

        @app.get("/admin/memory", include_in_schema=False)
        async def memory_admin() -> FileResponse:
            return FileResponse(admin_root / "memory.html")

    if chat_spa_ready:

        @app.get("/chat/manifest.webmanifest", include_in_schema=False)
        async def chat_manifest() -> FileResponse:
            return FileResponse(
                chat_dist / "manifest.webmanifest",
                media_type="application/manifest+json",
            )

        @app.get("/chat/sw.js", include_in_schema=False)
        async def chat_service_worker() -> FileResponse:
            return FileResponse(
                chat_dist / "sw.js",
                media_type="application/javascript",
                headers={"Cache-Control": "no-cache"},
            )

        @app.get("/chat/offline.html", include_in_schema=False)
        async def chat_offline() -> FileResponse:
            return FileResponse(chat_dist / "offline.html")

    @app.get("/chat", include_in_schema=False)
    @app.get("/chat/", include_in_schema=False)
    async def chat_entry() -> FileResponse:
        if chat_spa_ready:
            return FileResponse(chat_dist / "index.html")
        return FileResponse(chat_root / "index.html")

    @app.get("/chat/debug", include_in_schema=False)
    async def chat_debug() -> FileResponse:
        return FileResponse(chat_root / "index.html")

    @app.get("/healthz", tags=["system"])
    async def health() -> dict[str, object]:
        dispatcher = None
        status = "ok"
        if worker is not None:
            dispatcher = {
                "running": worker.state.running,
                "cycles": worker.state.cycles,
                "last_error": worker.state.last_error,
            }
            if not worker.state.running or worker.state.last_error is not None:
                status = "degraded"
        result: dict[str, object] = {"status": status, "version": __version__}
        if dispatcher is not None:
            result["dispatcher"] = dispatcher
        if runtime_config is not None:
            config_status = {
                "version": runtime_config.current.version,
                "content_hash": runtime_config.current.content_hash,
                "last_error": runtime_config.last_error,
            }
            result["configuration"] = config_status
            if runtime_config.last_error is not None:
                result["status"] = "degraded"
        if persona_store is not None:
            persona_snapshot = await persona_store.refresh()
            result["persona"] = {
                "version": persona_snapshot.version,
                "content_hash": persona_snapshot.content_hash,
                "name": persona_snapshot.persona.name,
            }
        if home_assistant_manager is not None:
            ha_health = home_assistant_manager.health()
            result["home_assistant"] = {
                "status": ha_health.status,
                "connected": ha_health.connected,
                "cached_entities": ha_health.cached_entities,
                "last_sync_at": (
                    ha_health.last_sync_at.isoformat()
                    if ha_health.last_sync_at is not None
                    else None
                ),
                "reason_code": ha_health.reason_code,
            }
            if ha_health.status in {"degraded", "auth_failed"}:
                result["status"] = "degraded"
        return result

    @app.get("/api/v1/meta/protocol", tags=["system"])
    async def protocol() -> dict[str, object]:
        return {
            "protocol_version": 1,
            "supported_protocol_versions": [1],
            "schemas": [
                "aria.input-envelope/1",
                "aria.agent-reply/1",
                "aria.output-intent/1",
            ],
        }

    @app.get("/api/v1/meta/runtime", tags=["system"])
    async def runtime_meta() -> dict[str, object]:
        result: dict[str, object] = {}
        if runtime_config is not None:
            # 前端据此决定定位授权节奏(每次询问/会话内允许); 不含任何密钥。
            result["location_policy"] = {
                "tools_enabled": runtime_config.current.config.tools.enabled,
                "precise": runtime_config.current.config.tools.query.precise_location_policy,
            }
        if persona_store is not None:
            persona_snapshot = await persona_store.refresh()
            result["persona"] = {
                "version": persona_snapshot.version,
                "content_hash": persona_snapshot.content_hash,
                "name": persona_snapshot.persona.name,
            }
            if avatar_store is not None:
                avatar = await avatar_store.get_default_for_persona(persona_snapshot.version)
                if avatar is not None:
                    pack = await avatar_store.get_pack(avatar.pack_id)
                    result["avatar"] = {
                        "instance_id": str(avatar.id),
                        "pack_id": avatar.pack_id,
                        "name": avatar.name,
                        "engine": pack.engine if pack is not None else "static",
                        "customization": avatar.customization,
                        "assets": pack.manifest.get("assets", {}) if pack is not None else {},
                    }
        return result

    @app.get("/api/v1/meta/adapters", tags=["system"])
    async def adapters() -> dict[str, object]:
        return {
            "adapters": [
                manifest.model_dump(mode="json") for manifest in runtime_adapters.list_manifests()
            ]
        }

    @app.get("/api/v1/meta/config", tags=["system"])
    async def configuration() -> dict[str, object]:
        if runtime_config is None:
            return {"configured": False}
        snapshot = runtime_config.current
        return {
            "configured": True,
            "version": snapshot.version,
            "content_hash": snapshot.content_hash,
            "models": [
                {
                    "name": name,
                    "kind": endpoint.kind,
                    "provider": endpoint.provider,
                    "model": endpoint.model,
                    "enabled": endpoint.enabled,
                    "runs_local": endpoint.runs_local,
                    "max_privacy_level": endpoint.max_privacy_level,
                }
                for name, endpoint in snapshot.config.models.items()
            ],
            "routes": {
                route: policy.model_dump(mode="json")
                for route, policy in snapshot.config.routes.items()
            },
            "capability_models": snapshot.config.capability_models.model_dump(mode="json"),
        }

    dev_enabled = enable_dev_endpoints
    if dev_enabled is None:
        dev_enabled = os.getenv("ARIA_ENABLE_DEV_ENDPOINTS", "false").lower() == "true"
    if dev_enabled and runtime_database is not None:
        app.include_router(create_event_router(runtime_database))
    if isinstance(runtime_config, DatabaseConfigStore):
        runtime_admin_token = (
            admin_token if admin_token is not None else os.getenv("ARIA_ADMIN_TOKEN")
        )
        set_runtime_admin_token(runtime_admin_token)

        async def reconfigure_integrations() -> None:
            if home_assistant_manager is not None:
                await home_assistant_manager.reconfigure()
            if xiaoai_materializer is not None:
                await xiaoai_materializer.write()

        async def trigger_calendar_sync(provider: str) -> dict[str, object]:
            """Admin 手动同步外部日历；两个镜像服务等价，读各自配置门控。"""
            service = caldav_sync_service if provider == "caldav" else google_calendar_sync_service
            if service is None:
                raise RuntimeError("calendar sync service unavailable")
            if provider == "caldav":
                stats = await service.sync_once()
            else:
                stats = await service.sync_once()
            return {
                "calendars": stats.calendars,
                "pulled": stats.pulled,
                "mirrors_created": stats.mirrors_created,
                "mirrors_updated": stats.mirrors_updated,
                "mirrors_cancelled": stats.mirrors_cancelled,
                "errors": stats.errors,
            }

        app.include_router(
            create_admin_config_router(
                runtime_config,
                admin_token=runtime_admin_token,
                on_publish=reconfigure_integrations,
                on_proactive_test=test_home_assistant_proactive,
            )
        )
        app.include_router(
            create_admin_senseaudio_router(
                runtime_config,
                admin_token=runtime_admin_token,
            )
        )
        app.include_router(
            create_admin_mcp_router(
                mcp_manager,
                admin_token=runtime_admin_token,
            )
        )
        app.state.mcp_manager = mcp_manager
        if mcp_manager is not None:
            # MCP-D：目录刷新后把白名单写工具同步进动作注册表（A2 每次确认）
            mcp_manager.set_catalog_listener(lambda: sync_mcp_actions(action_registry, mcp_manager))
        if persona_store is not None:
            app.include_router(
                create_admin_persona_router(
                    persona_store,
                    admin_token=runtime_admin_token,
                )
            )
        if timeline_store is not None:
            app.include_router(
                create_admin_timeline_router(
                    timeline_store,
                    admin_token=runtime_admin_token,
                )
            )
        if memory_store is not None:
            app.include_router(
                create_admin_memory_router(
                    memory_store,
                    admin_token=runtime_admin_token,
                )
            )
            app.include_router(
                create_deletion_ledger_router(
                    memory_store,
                    admin_token=runtime_admin_token,
                )
            )
        # BK-01 备份状态：目录只读检视，不依赖数据库
        app.include_router(
            create_admin_backups_router(
                backup_dir=Path(os.getenv("ARIA_BACKUP_DIR", "backups")),
                keep_days=int(os.getenv("ARIA_BACKUP_KEEP_DAYS", "14")),
                backup_at=os.getenv("ARIA_BACKUP_AT", "03:30"),
                timezone_name=os.getenv("ARIA_BACKUP_TZ", "Asia/Shanghai"),
                admin_token=runtime_admin_token,
            )
        )
        if runtime_database is not None:
            # FR-S3 数据导出/导入：伴侣数据 JSON 档案，凭据与机器状态不导出
            app.include_router(
                create_admin_export_router(
                    database=runtime_database,
                    admin_token=runtime_admin_token,
                    hub_version=__version__,
                )
            )
            app.include_router(
                create_admin_dashboard_router(
                    runtime_database,
                    admin_token=runtime_admin_token,
                    version=__version__,
                )
            )
            app.include_router(
                create_logs_stream_router(
                    admin_token=runtime_admin_token,
                )
            )
            if job_engine is not None:
                app.include_router(
                    create_admin_jobs_router(
                        job_engine,
                        admin_token=runtime_admin_token,
                    )
                )
            if task_store is not None:
                app.include_router(
                    create_admin_tasks_router(
                        task_store,
                        admin_token=runtime_admin_token,
                    )
                )
            if (
                workflow_service is not None
                and home_scene_service is not None
                and meeting_service is not None
                and daily_brief_service is not None
                and daily_review_service is not None
            ):
                app.include_router(
                    create_admin_butler_router(
                        database=runtime_database,
                        workflows=workflow_service,
                        scenes=home_scene_service,
                        meetings=meeting_service,
                        briefs=daily_brief_service,
                        reviews=daily_review_service,
                        admin_token=runtime_admin_token,
                    )
                )
            app.include_router(
                create_admin_screen_awareness_router(
                    timeline_store,
                    admin_token=runtime_admin_token,
                )
            )
            app.include_router(
                create_admin_browser_awareness_router(
                    timeline_store,
                    admin_token=runtime_admin_token,
                )
            )
            if avatar_store is not None:
                app.include_router(
                    create_admin_avatar_router(
                        avatar_store,
                        admin_token=runtime_admin_token,
                        asset_importer=avatar_importer,
                    )
                )
            if theme_store is not None:
                app.include_router(
                    create_admin_theme_router(
                        theme_store,
                        admin_token=runtime_admin_token,
                    )
                )
            auth_service = AuthService(
                runtime_database,
                # 会话滑动续期：活跃用户不再每 8 小时被强制登出；
                # 硬顶期内到期自动顺延，超过硬顶仍需重新登录。
                session_ttl=timedelta(hours=float(os.getenv("ARIA_SESSION_TTL_HOURS", "8"))),
                session_max_lifetime=timedelta(
                    days=float(os.getenv("ARIA_SESSION_MAX_LIFETIME_DAYS", "7"))
                ),
            )
            app.include_router(
                create_admin_security_router(
                    admin_token=runtime_admin_token,
                    auth_service=auth_service,
                )
            )
            device_tools: list[ToolHandler] = []
            calendar_create_tool: CalendarCreateTool | None = None
            workflow_save_tool: WorkflowSaveTool | None = None
            capability_providers: list[RuntimeCapabilityProvider] = []
            device_target_resolver: DeviceTargetResolver | None = None
            device_command_gateway = None
            if device_registry is not None:
                capability_providers.append(device_registry)
                device_target_resolver = DeviceTargetResolver(device_registry)
                admin_devices_router, devices_router = create_device_routers(
                    device_registry,
                    admin_token=runtime_admin_token,
                )
                app.include_router(admin_devices_router)
                app.include_router(devices_router)
                if device_command_store is not None:
                    command_admin_router, device_ws_router, device_command_gateway = (
                        create_device_command_routers(
                            device_registry,
                            device_command_store,
                            admin_token=runtime_admin_token,
                        )
                    )
                    app.state.device_command_gateway = device_command_gateway
                    app.include_router(command_admin_router)
                    app.include_router(device_ws_router)
                    if capability_models is not None:
                        screen_analyzer = CapabilityScreenAnalyzer(capability_models)
                        device_tools.append(
                            CaptureScreenTool(
                                device_target_resolver,
                                device_command_gateway,
                                screen_analyzer,
                            )
                        )
                        device_tools.append(
                            InspectWebpageTool(
                                device_target_resolver,
                                device_command_gateway,
                                screen_analyzer,
                            )
                        )
            if home_assistant_manager is not None:
                capability_providers.append(home_assistant_manager)
                device_tools.append(SearchDevicesTool(home_assistant_manager))
                device_tools.append(HomeGetStateTool(home_assistant_manager))
                device_tools.append(HomeGetHistoryTool(home_assistant_manager))
                device_tools.append(HomeControlTool(home_assistant_manager))
            device_tools.append(
                ReadSensorsTool(
                    ha_provider=home_assistant_manager,
                    mqtt_buffer=mqtt_client.buffer if mqtt_client is not None else None,
                )
            )
            default_timezone = os.getenv("ARIA_DEFAULT_TIMEZONE", "Asia/Shanghai")
            # FIX-01/01B 确认预览统一持久化：跨重启/跨 worker 存活
            pending_mutations = (
                DatabasePendingMutationStore(runtime_database) if runtime_database else None
            )
            if task_store is not None:
                device_tools.append(ReminderCreateTool(task_store, timezone_name=default_timezone))
            if calendar_service is not None:
                calendar_create_tool = CalendarCreateTool(
                    calendar_service,
                    timezone_name=default_timezone,
                    drafts=pending_mutations,
                )
                device_tools.append(calendar_create_tool)
            device_tools.append(
                CalendarSyncTool(
                    caldav_sync=caldav_sync_service,
                    google_sync=google_calendar_sync_service,
                )
            )
            if contact_store is not None:
                device_tools.append(ContactSaveTool(contact_store))
                device_tools.append(ContactQueryTool(contact_store))
            device_tools.append(CommuteCheckTool(build_commute_service))
            if workflow_service is not None:
                workflow_save_tool = WorkflowSaveTool(workflow_service, drafts=pending_mutations)
                device_tools.append(workflow_save_tool)
                device_tools.append(WorkflowRunTool(workflow_service))
            if focus_service is not None:
                device_tools.extend(
                    [
                        FocusStartTool(focus_service),
                        FocusStopTool(focus_service),
                        FocusStatusTool(focus_service),
                    ]
                )
            if home_scene_service is not None:
                device_tools.extend(
                    [
                        HomeSceneRunTool(home_scene_service),
                        HomeSceneListTool(home_scene_service),
                    ]
                )
            mail_attachments = (
                MailAttachmentStore(runtime_database)
                if runtime_config is not None and runtime_database
                else None
            )
            if runtime_config is not None:
                device_tools.extend(
                    create_mail_tools(
                        runtime_config, drafts=pending_mutations, attachments=mail_attachments
                    )
                )
            if pnkx_life_client is not None:
                current_pnkx_base_url, _, _ = pnkx_settings()
                pnkx_is_local = pnkx_runs_local(current_pnkx_base_url)
                device_tools.append(PnkxReadTool(pnkx_life_client, runs_local=pnkx_is_local))
                # 写权限由动态客户端在每次请求时按当前数据库配置执行；始终注册工具，
                # 后台启用/停用后无需重建 ChatService。
                device_tools.append(PnkxCreateTool(pnkx_life_client, runs_local=pnkx_is_local))
            if action_plan_service is not None:
                if device_target_resolver is not None and device_command_gateway is not None:
                    device_tools.append(
                        DesktopNotifyTool(device_target_resolver, device_command_gateway)
                    )
                    if runtime_config is not None:

                        def desktop_actions_config() -> DesktopActionsConfig:
                            return runtime_config.current.config.tools.desktop_actions

                        device_tools.extend(
                            [
                                DesktopOpenAppTool(
                                    device_target_resolver,
                                    device_command_gateway,
                                    desktop_actions_config,
                                ),
                                DesktopOpenUrlTool(
                                    device_target_resolver,
                                    device_command_gateway,
                                    desktop_actions_config,
                                ),
                                DesktopSetVolumeTool(
                                    device_target_resolver,
                                    device_command_gateway,
                                    desktop_actions_config,
                                ),
                                DesktopClipboardWriteTool(
                                    device_target_resolver,
                                    device_command_gateway,
                                    desktop_actions_config,
                                ),
                            ]
                        )
                    if mcp_manager is not None:
                        # MCP-D：唯一写调用入口，注册到计划执行器；聊天挂载恒关
                        device_tools.append(McpToolCallTool(mcp_manager))
                    if runtime_config is not None:

                        def browser_workflow_config() -> BrowserWorkflowConfig:
                            return runtime_config.current.config.tools.browser_workflow

                        device_tools.extend(
                            [
                                BrowserOpenTabTool(
                                    device_target_resolver,
                                    device_command_gateway,
                                    browser_workflow_config,
                                ),
                                BrowserFormReadTool(
                                    device_target_resolver,
                                    device_command_gateway,
                                    browser_workflow_config,
                                ),
                                BrowserFormFillTool(
                                    device_target_resolver,
                                    device_command_gateway,
                                    browser_workflow_config,
                                ),
                                BrowserFormSubmitTool(
                                    device_target_resolver,
                                    device_command_gateway,
                                    browser_workflow_config,
                                ),
                            ]
                        )
                action_plan_service.set_runner(
                    ToolActionRunner(
                        ToolExecutor(ToolRegistry(device_tools)),
                        home_state_provider=home_assistant_manager,
                    )
                )
            capability_provider = (
                CompositeRuntimeCapabilityProvider(capability_providers)
                if capability_providers
                else None
            )
            runtime_chat_service = ChatService(
                runtime_database,
                runtime_config,
                persona_store=persona_store,
                memory_store=memory_store,
                memory_extractor=memory_extractor,
                timeline_store=timeline_store,
                history_recall_service=history_recall,
                capability_provider=capability_provider,
                device_tools=device_tools,
                mcp_tools=(McpChatToolProvider(mcp_manager) if mcp_manager is not None else None),
                cognitive_cycle=cognitive_cycle,
                avatar_store=avatar_store,
                goal_tracker=goal_tracker,
            )
            app.state.auth_service = auth_service
            app.state.chat_service = runtime_chat_service
            app.include_router(create_auth_router(auth_service, admin_token=runtime_admin_token))
            app.include_router(create_chat_router(runtime_chat_service, auth_service))
            for device_tool in device_tools:
                if isinstance(device_tool, MailSendTool):
                    app.include_router(
                        create_mail_router(device_tool, auth_service, attachments=mail_attachments)
                    )
            if avatar_store is not None and persona_store is not None:
                app.include_router(create_avatar_router(avatar_store, persona_store, auth_service))
            if theme_store is not None:
                app.include_router(create_theme_router(theme_store, auth_service))
            if cognitive_store is not None:
                app.include_router(
                    create_cognition_router(
                        cognitive_store,
                        auth_service,
                        perception_store,
                        action_registry,
                        action_plan_service,
                    )
                )
            if task_store is not None:
                app.include_router(create_tasks_router(task_store, auth_service))
                app.state.task_store = task_store
                app.state.task_scheduler = task_scheduler
                app.state.goal_reminder_scheduler = goal_reminder_scheduler
            if focus_service is not None:
                app.state.focus_service = focus_service
                app.state.focus_scheduler = focus_scheduler
            if daily_brief_service is not None:
                app.include_router(create_briefs_router(daily_brief_service, auth_service))
                app.state.daily_brief_service = daily_brief_service
                app.state.daily_brief_scheduler = daily_brief_scheduler
            if daily_review_service is not None:
                app.include_router(create_reviews_router(daily_review_service, auth_service))
                app.state.daily_review_service = daily_review_service
                app.state.daily_review_scheduler = daily_review_scheduler
            if calendar_service is not None:
                app.include_router(
                    create_calendar_router(
                        calendar_service,
                        auth_service,
                        calendar_create_tool,
                        caldav_sync=caldav_sync_service,
                        google_sync=google_calendar_sync_service,
                        google_state_key=os.getenv("ARIA_ADMIN_TOKEN") or None,
                    )
                )
                app.state.calendar_service = calendar_service
                app.state.caldav_sync_service = caldav_sync_service
            if meeting_service is not None:
                app.include_router(create_meetings_router(meeting_service, auth_service))
                app.state.meeting_service = meeting_service
            if contact_store is not None:
                app.include_router(create_contacts_router(contact_store, auth_service))
                if safety_alert_service is not None:
                    app.include_router(create_safety_router(safety_alert_service, auth_service))
                app.state.contact_store = contact_store
            if home_scene_service is not None:
                app.include_router(create_home_scenes_router(home_scene_service, auth_service))
                app.state.home_scene_service = home_scene_service
            if workflow_service is not None:
                app.include_router(
                    create_workflows_router(workflow_service, auth_service, workflow_save_tool)
                )
                app.state.workflow_service = workflow_service
            if todo_sync_service is not None:
                app.include_router(create_todo_router(todo_sync_service, auth_service))
                app.state.todo_sync_service = todo_sync_service
                app.state.todo_sync_scheduler = todo_sync_scheduler
            if pnkx_life_client is not None:
                app.include_router(
                    create_pnkx_router(
                        pnkx_life_client,
                        auth_service,
                        writes_enabled=True,
                    )
                )
                app.state.pnkx_life_client = pnkx_life_client
            if capability_models is not None:
                app.include_router(create_model_capability_router(capability_models, auth_service))
            turn_coordinator = TurnCoordinator(runtime_database, runtime_chat_service)
            websocket_router, websocket_manager = create_chat_websocket_router(
                runtime_chat_service,
                auth_service,
                turn_coordinator=turn_coordinator,
                avatar_control_publisher=device_command_gateway,
            )
            app.state.chat_websocket_manager = websocket_manager
            if action_plan_service is not None:

                async def push_plan_execution(event: PlanExecutionEvent) -> None:
                    # 与其他 Chat WS 帧一致的信封：客户端按 event.payload 取字段。
                    await websocket_manager.broadcast_to_user(
                        event.user_id,
                        {
                            "type": "plan.execution",
                            "payload": event.model_dump(mode="json"),
                        },
                    )

                async def push_plan_changed(event: PlanChangedEvent) -> None:
                    await websocket_manager.broadcast_to_user(
                        event.user_id,
                        {"type": "plan.changed", "payload": event.model_dump(mode="json")},
                    )

                action_plan_service.set_execution_listener(push_plan_execution)
                action_plan_service.set_change_listener(push_plan_changed)
            if device_command_gateway is not None:
                device_command_gateway.set_pet_message_handler(
                    websocket_manager.submit_device_message
                )
            app.include_router(websocket_router)
            if xiaoai_materializer is not None:
                xiaoai_router, xiaoai_manager = create_xiaoai_websocket_router(
                    runtime_chat_service,
                    credentials_provider=xiaoai_materializer.credentials,
                )
                app.state.xiaoai_websocket_manager = xiaoai_manager
                app.include_router(xiaoai_router)
            voice_manager = None
            if runtime_config is not None:
                voice_router, voice_manager = create_voice_websocket_router(
                    runtime_chat_service,
                    auth_service,
                    voice_source=ConfigVoiceSource(runtime_config),
                    turn_coordinator=turn_coordinator,
                    avatar_control_publisher=device_command_gateway,
                )
                app.state.voice_websocket_manager = voice_manager
                app.include_router(
                    create_admin_voice_router(
                        voice_manager.latency_report,
                        voice_manager.reset_latency_metrics,
                        admin_token=runtime_admin_token,
                    )
                )
                if device_command_gateway is not None:
                    device_command_gateway.set_pet_audio_handler(voice_manager.stream_device_speech)
                    device_command_gateway.set_satellite_utterance_handler(
                        voice_manager.run_satellite_utterance
                    )
                    device_command_gateway.set_satellite_takeover_handler(
                        voice_manager.transfer_satellite_conversation
                    )
                    voice_manager.set_satellite_broadcaster(
                        device_command_gateway.broadcast_satellite
                    )
                app.include_router(voice_router)
            push_subscription_store = (
                PushSubscriptionStore(runtime_database) if runtime_database is not None else None
            )
            if push_subscription_store is not None:
                app.include_router(
                    create_push_router(push_subscription_store, runtime_config, auth_service)
                )
                app.state.push_subscription_store = push_subscription_store
            if runtime_config is not None:
                # 主动投递栈只依赖配置/数据库/通道组件，不依赖 Home Assistant；
                # 误挂在 HA 条件下会在 HA 缺席时让提醒/简报/回顾/安全告警全部静默失效。
                proactive_delivery = ProactiveDeliveryService(
                    runtime_database,
                    runtime_config,
                    runtime_chat_service,
                    websocket_manager,
                    device_resolver=device_target_resolver,
                    device_gateway=(
                        cast(DesktopCommandGateway, device_command_gateway)
                        if device_command_gateway is not None
                        else None
                    ),
                    voice_broadcaster=voice_manager,
                    push_adapter=(
                        WebPushAdapter(push_subscription_store, config_store=runtime_config)
                        if push_subscription_store is not None
                        else None
                    ),
                )
                app.state.proactive_delivery_service = proactive_delivery
                # SAFE-02：告警状态机（critical 升级链 + 聊天确认意图 + Timeline）
                safety_alert_service = SafetyAlertService(
                    runtime_database,
                    runtime_config,
                    proactive_delivery.deliver,
                    timeline=timeline_store,
                    mailer=MailClient(runtime_config),
                )
                app.state.safety_alert_service = safety_alert_service
                runtime_chat_service.set_safety(safety_alert_service)
                runtime_chat_service.set_activity_tracker(activity_tracker)
                app.include_router(
                    create_admin_safety_router(
                        safety_alert_service, admin_token=runtime_admin_token
                    )
                )
                # SAFE-01：久未活动调度器（外部信号可经 activity_tracker.record 注入）
                activity_scheduler = SafetyActivityScheduler(
                    runtime_database,
                    runtime_config,
                    proactive_delivery.deliver,
                    activity_tracker,
                    cognitive_cycle=cognitive_cycle,
                )
                app.state.safety_activity_scheduler = activity_scheduler
                app.state.safety_activity_tracker = activity_tracker
                if task_scheduler is not None:
                    task_scheduler.set_deliverer(deliver_task_reminder)
                if goal_reminder_scheduler is not None:
                    goal_reminder_scheduler.set_deliverer(deliver_goal_reminder)
                if focus_scheduler is not None:

                    async def deliver_focus_nudge(
                        text: str,
                        *,
                        user_id: UUID,
                        entity_id: str,
                        trigger_kind: str,
                        privacy_level: str,
                    ) -> list[str] | None:
                        result = await proactive_delivery.deliver(
                            text,
                            entity_id=entity_id,
                            rule_id=entity_id,
                            trigger_kind=trigger_kind,
                            privacy_level=PrivacyLevel(privacy_level),
                            target_user_id=user_id,
                        )
                        if result is None:
                            return None
                        return list(result.delivered_channels)

                    focus_scheduler.set_deliverer(deliver_focus_nudge)
                if daily_brief_scheduler is not None:

                    async def deliver_daily_brief(
                        text: str,
                        *,
                        user_id: UUID,
                        brief_id: UUID,
                        privacy_level: PrivacyLevel,
                        trigger_kind: str,
                    ) -> list[str] | None:
                        if proactive_delivery is None:
                            return None
                        result = await proactive_delivery.deliver(
                            text,
                            entity_id="brief",
                            rule_id=f"brief:{brief_id}",
                            trigger_kind=trigger_kind,
                            privacy_level=privacy_level,
                            target_user_id=user_id,
                        )
                        if result is None:
                            return None
                        return list(result.delivered_channels)

                    daily_brief_scheduler.set_deliverer(deliver_daily_brief)
                if daily_review_scheduler is not None:

                    async def deliver_daily_review(
                        text: str,
                        *,
                        user_id: UUID,
                        review_id: UUID,
                        privacy_level: PrivacyLevel,
                        trigger_kind: str,
                    ) -> list[str] | None:
                        if proactive_delivery is None:
                            return None
                        result = await proactive_delivery.deliver(
                            text,
                            entity_id="review",
                            rule_id=f"review:{review_id}",
                            trigger_kind=trigger_kind,
                            privacy_level=privacy_level,
                            target_user_id=user_id,
                        )
                        if result is None:
                            return None
                        return list(result.delivered_channels)

                    daily_review_scheduler.set_deliverer(deliver_daily_review)

            if home_assistant_manager is not None:
                home_assistant_proactive = HomeAssistantProactiveEngine(
                    runtime_database,
                    runtime_config,
                    home_assistant_manager.get_state,
                    proactive_delivery.deliver,
                    cognitive_cycle=cognitive_cycle,
                    perception_pipeline=perception_pipeline,
                    safety=safety_alert_service,
                )
                home_assistant_manager.set_state_change_handler(
                    home_assistant_proactive.on_state_change
                )
                app.state.home_assistant_proactive_engine = home_assistant_proactive

            if mqtt_client is not None and perception_pipeline is not None:
                mqtt_presence_bridge = MqttPresenceBridge(
                    runtime_database,
                    perception_pipeline,
                    proactive_deliver=(
                        proactive_delivery.deliver if proactive_delivery is not None else None
                    ),
                )
                mqtt_client.set_signal_handler(mqtt_presence_bridge.handle)
                app.state.mqtt_presence_bridge = mqtt_presence_bridge

            if (
                runtime_database is not None
                and timeline_store is not None
                and device_target_resolver is not None
                and device_command_gateway is not None
                and capability_models is not None
            ):
                screen_awareness_loop = ScreenAwarenessLoop(
                    config_store=runtime_config,
                    database=runtime_database,
                    resolver=cast(ScreenAwarenessResolver, device_target_resolver),
                    gateway=cast(ScreenAwarenessGateway, device_command_gateway),
                    analyzer=cast(
                        ScreenAwarenessAnalyzer, CapabilityScreenAnalyzer(capability_models)
                    ),
                    timeline=timeline_store,
                    memory_ingester=(
                        MemoryIngester(memory_store) if memory_store is not None else None
                    ),
                    perception_pipeline=perception_pipeline,
                    proactive_deliver=(
                        proactive_delivery.deliver if proactive_delivery is not None else None
                    ),
                    cognitive_cycle=cognitive_cycle,
                )
                app.state.screen_awareness_loop = screen_awareness_loop
                browser_awareness_loop = BrowserAwarenessLoop(
                    config_store=runtime_config,
                    database=runtime_database,
                    resolver=cast(BrowserAwarenessResolver, device_target_resolver),
                    gateway=cast(BrowserAwarenessGateway, device_command_gateway),
                    analyzer=cast(BrowserAwarenessAnalyzer, LlmBrowserAnalyzer(runtime_config)),
                    timeline=timeline_store,
                    memory_ingester=(
                        MemoryIngester(memory_store) if memory_store is not None else None
                    ),
                    perception_pipeline=perception_pipeline,
                    proactive_deliver=(
                        proactive_delivery.deliver if proactive_delivery is not None else None
                    ),
                    cognitive_cycle=cognitive_cycle,
                    tab_hints=cast(BrowserAwarenessTabHints, device_command_gateway),
                )
                app.state.browser_awareness_loop = browser_awareness_loop

    return app


app = create_app()
