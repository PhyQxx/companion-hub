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

from app import __version__
from app.adapters import AdapterRegistry
from app.adapters.builtin import create_builtin_registry
from app.api import (
    create_admin_avatar_router,
    create_admin_browser_awareness_router,
    create_admin_butler_router,
    create_admin_dashboard_router,
    create_admin_export_router,
    create_admin_jobs_router,
    create_admin_screen_awareness_router,
    create_admin_security_router,
    create_admin_tasks_router,
    create_admin_theme_router,
    create_admin_voice_router,
    create_auth_router,
    create_avatar_router,
    create_briefs_router,
    create_calendar_router,
    create_chat_router,
    create_chat_websocket_router,
    create_cognition_router,
    create_contacts_router,
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
from app.api.mail import create_mail_router
from app.appearance import ThemeStore
from app.auth import AuthService
from app.avatar import AvatarAssetImporter, AvatarStore
from app.browser_awareness import (
    BrowserAwarenessLoop,
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
    PlanCompletionReporter,
    PlanExecutionEvent,
    ProposeActionTool,
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
from app.integrations.mcp.chat_tools import McpChatToolProvider
from app.jobs import (
    DELEG_KIND_RESEARCH,
    AssetStore,
    DelegatedJobWorker,
    DelegateTaskTool,
    JobEngine,
    WebResearchHandler,
)
from app.llm.factory import build_router
from app.llm.provider import EnvSecretProvider
from app.mail import (
    MailAttachmentStore,
    MailOutboxStore,
    MailSendTool,
    create_mail_tools,
)
from app.mail_awareness import MailAwarenessLoop
from app.meetings import LlmMeetingSummarizer, MeetingService, MeetingStore
from app.memory import (
    LlmMemoryExtractor,
    MemoryExtractor,
    MemoryRetriever,
    MemoryStore,
    build_embedding_provider,
    probe_embedding_provider,
)
from app.model_capabilities import CapabilityModelService
from app.observability import apply_observability, configure_logging
from app.output import ProactiveDeliveryService
from app.perception import PerceptionPipeline, PerceptionStore, ProactivePolicy
from app.perception.pipeline import EventObserver
from app.persona import PersonaStore
from app.pnkx import PnkxLifeClient
from app.push import PushSubscriptionStore
from app.runtime import TurnCoordinator
from app.safety import ActivityTracker, SafetyActivityScheduler, SafetyAlertService
from app.schemas.common import PrivacyLevel
from app.screen_awareness import (
    ScreenAwarenessLoop,
)
from app.skills.actions import sync_skill_actions
from app.skills.connections import SkillConnectionStore, SkillHttpClient
from app.skills.credentials import SkillCredentialStore
from app.skills.drafts import SkillDraftAssistant
from app.skills.generator import SkillDraftGenerator
from app.skills.runtime import SkillToolProvider
from app.skills.store import SkillStore
from app.skills.writes import SkillWriteToolHandler
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
    FetchWebpageTool,
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
from app.wiring.admin_routers import register_admin_routers
from app.wiring.frontend import register_frontend
from app.wiring.proactive import register_awareness_loops, register_proactive_stack
from app.workflows import (
    WorkflowRunTool,
    WorkflowSaveTool,
    WorkflowService,
    WorkflowStore,
)
from app.workflows.drafts import PlanDistiller, WorkflowDraftStore
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
    mail_awareness_loop: MailAwarenessLoop | None = None
    mcp_manager = McpManager(runtime_config) if runtime_config is not None else None
    skill_store = SkillStore(runtime_database) if runtime_database is not None else None
    skill_connections = (
        SkillConnectionStore(runtime_database) if runtime_database is not None else None
    )
    skill_credentials = (
        SkillCredentialStore(runtime_database) if runtime_database is not None else None
    )
    skill_http_client = (
        SkillHttpClient(skill_connections, credentials=skill_credentials)
        if skill_connections is not None
        else None
    )
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
    # DIST（docs/09 §4）：计划轨迹蒸馏为流程草稿；回放执行器随 device_tools
    # 装配完成后注入，完成回调只负责触发。命名经 utility 路由润色，失败
    # 回落确定性标题命名。
    workflow_draft_store = (
        WorkflowDraftStore(runtime_database) if runtime_database is not None else None
    )
    _distill_secrets = EnvSecretProvider()
    plan_distiller = (
        PlanDistiller(
            runtime_database,
            workflow_draft_store,
            registry=action_registry,
            config_store=runtime_config,
            router_builder=(
                lambda config: build_router(config, _distill_secrets)
                if runtime_config is not None
                else None
            ),
        )
        if runtime_database is not None and workflow_draft_store is not None
        else None
    )
    if action_plan_service is not None and plan_distiller is not None:
        action_plan_service.add_completion_callback(plan_distiller.on_plan_completed)
    # BTL-03（docs/09 §1）：对话内提议注册表动作（A1/A2 转计划确认）+
    # 计划完成主动汇报（续跑闭环）。
    plan_completion_reporter = (
        PlanCompletionReporter(action_plan_service)
        if action_plan_service is not None
        else None
    )
    if action_plan_service is not None and plan_completion_reporter is not None:
        action_plan_service.add_completion_callback(
            plan_completion_reporter.on_plan_completed
        )
    propose_action_tool = (
        ProposeActionTool(
            lambda: action_plan_service,
            action_registry,
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
    skill_tool_provider = (
        SkillToolProvider(
            skill_store,
            connections=skill_connections,
            http_client=skill_http_client,
        )
        if skill_store is not None
        else None
    )
    skill_generator = SkillDraftGenerator(runtime_config) if runtime_config is not None else None
    # 只读网页抓取工具：配置实时读取，挂载与隐私门在 ChatService 内按回合判定
    web_fetch_tool = (
        FetchWebpageTool(lambda: runtime_config.current.config.tools.web_fetch)
        if runtime_config is not None
        else None
    )
    # DELEG（docs/09 §5）：长任务委派 worker + 对话入口工具；主动汇报通道
    # 在 ProactiveDeliveryService 装配后注入。
    deleg_worker = (
        DelegatedJobWorker(job_engine)
        if job_engine is not None
        and runtime_config is not None
        and web_fetch_tool is not None
        else None
    )
    if deleg_worker is not None and web_fetch_tool is not None and runtime_config is not None:
        deleg_worker.register(
            DELEG_KIND_RESEARCH, WebResearchHandler(web_fetch_tool, runtime_config)
        )
    delegate_task_tool = DelegateTaskTool(job_engine) if job_engine is not None else None
    # 对话内 propose_skill 工具 + 回合后草稿收割，共用生成器与草稿存储；
    # url 文档链接由服务端复用 web_fetch 抓取，避免模型转述正文
    skill_draft_assistant = (
        SkillDraftAssistant(skill_store, skill_generator, web_fetch=web_fetch_tool)
        if skill_store is not None and skill_generator is not None
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
        if memory_store is not None and runtime_config is not None:
            # SEMB（docs/09 §2）：配置启用语义嵌入时先做连通性探测，
            # 失败回落内置哈希嵌入（词法级检索），不阻断启动。
            embedding_provider = build_embedding_provider(
                runtime_config.current.config.embeddings, EnvSecretProvider()
            )
            if embedding_provider is not None:
                if await probe_embedding_provider(embedding_provider):
                    memory_store.set_embedding_provider(embedding_provider)
                    logging.getLogger(__name__).info(
                        "semantic embedding enabled model=%s dimension=%d",
                        embedding_provider.model_name,
                        embedding_provider.dimension,
                    )
                else:
                    logging.getLogger(__name__).warning(
                        "semantic embedding unavailable, falling back to hashing embedder"
                    )
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
        if deleg_worker is not None:
            deleg_worker.start()
        if screen_awareness_loop is not None:
            screen_awareness_loop.start()
        if browser_awareness_loop is not None:
            browser_awareness_loop.start()
        if mail_awareness_loop is not None:
            mail_awareness_loop.start()
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
        if skill_store is not None and action_registry is not None:
            # 技能 S3：启动时把已启用技能的写操作同步进动作目录
            try:
                sync_skill_actions(action_registry, await skill_store.list())
            except Exception:
                logging.getLogger(__name__).warning(
                    "skill write action sync failed on startup", exc_info=True
                )
        try:
            yield
        finally:
            if skill_http_client is not None:
                await skill_http_client.close()
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
            if mail_awareness_loop is not None:
                await mail_awareness_loop.stop()
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
            if deleg_worker is not None:
                await deleg_worker.stop()
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
    app.state.skill_store = skill_store
    app.state.skill_connections = skill_connections

    # PERE-03：前端静态资源与系统端点搬至 wiring/frontend.py（行为不变）
    register_frontend(
        app,
        worker=worker,
        config_store=runtime_config,
        persona_store=persona_store,
        avatar_store=avatar_store,
        home_assistant_manager=home_assistant_manager,
        avatar_upload_root=avatar_upload_root,
        adapters=runtime_adapters,
        database=runtime_database,
        enable_dev_endpoints=enable_dev_endpoints,
    )
    # PERE-03：Admin 前置路由搬至 wiring/admin_routers.py（行为不变，
    # 原 trigger_calendar_sync 死代码删除）
    runtime_admin_token: str | None = None
    if isinstance(runtime_config, DatabaseConfigStore):
        runtime_admin_token = register_admin_routers(
            app,
            config=runtime_config,
            admin_token=admin_token,
            home_assistant_manager=home_assistant_manager,
            xiaoai_materializer=xiaoai_materializer,
            ha_proactive_test=test_home_assistant_proactive,
            mcp_manager=mcp_manager,
            skill_store=skill_store,
            skill_generator=skill_generator,
            skill_tool_provider=skill_tool_provider,
            skill_connections=skill_connections,
            skill_credentials=skill_credentials,
            skill_http_client=skill_http_client,
            action_registry=action_registry,
            persona_store=persona_store,
            timeline_store=timeline_store,
            memory_store=memory_store,
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
                        drafts=workflow_draft_store,
                        distiller=plan_distiller,
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
            if delegate_task_tool is not None:
                device_tools.append(delegate_task_tool)
            if propose_action_tool is not None:
                device_tools.append(propose_action_tool)
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
            mail_outbox = MailOutboxStore(runtime_database) if runtime_database else None
            if runtime_config is not None:
                device_tools.extend(
                    create_mail_tools(
                        runtime_config,
                        drafts=pending_mutations,
                        attachments=mail_attachments,
                        outbox=mail_outbox,
                    )
                )
            if (
                skill_store is not None
                and skill_connections is not None
                and skill_http_client is not None
            ):
                # 技能 S3 写动作执行器：仅经 Action Registry 计划—确认—执行链触发，
                # 不作为聊天工具挂载（chat 侧 _device_tool_ready 黑名单）。
                device_tools.append(
                    SkillWriteToolHandler(skill_store, skill_connections, skill_http_client)
                )
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
                if plan_distiller is not None:
                    # DIST：回放复用与计划执行同一套工具注册表与隐私闸门
                    plan_distiller.set_executor(ToolExecutor(ToolRegistry(device_tools)))
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
                skill_tools=skill_tool_provider,
                skill_drafts=skill_draft_assistant,
                web_fetch=web_fetch_tool,
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
                # PERE-03：主动投递栈/HA 主动引擎/MQTT 桥/感知循环搬至
                # wiring/proactive.py（行为不变）
                (
                    proactive_delivery,
                    safety_alert_service,
                    activity_scheduler,
                    home_assistant_proactive,
                    mqtt_presence_bridge,
                ) = register_proactive_stack(
                    app,
                    config=runtime_config,
                    database=runtime_database,
                    chat_service=runtime_chat_service,
                    websocket_manager=websocket_manager,
                    device_target_resolver=device_target_resolver,
                    device_command_gateway=device_command_gateway,
                    voice_manager=voice_manager,
                    push_subscription_store=push_subscription_store,
                    deleg_worker=deleg_worker,
                    plan_completion_reporter=plan_completion_reporter,
                    activity_tracker=activity_tracker,
                    job_engine=job_engine,
                    admin_token=runtime_admin_token,
                    cognitive_cycle=cognitive_cycle,
                    task_scheduler=task_scheduler,
                    goal_reminder_scheduler=goal_reminder_scheduler,
                    focus_scheduler=focus_scheduler,
                    daily_brief_scheduler=daily_brief_scheduler,
                    daily_review_scheduler=daily_review_scheduler,
                    timeline_store=timeline_store,
                    home_assistant_manager=home_assistant_manager,
                    mqtt_client=mqtt_client,
                    perception_pipeline=perception_pipeline,
                )
            if (
                runtime_database is not None
                and timeline_store is not None
                and device_target_resolver is not None
                and device_command_gateway is not None
                and capability_models is not None
                and runtime_config is not None
            ):
                (
                    screen_awareness_loop,
                    browser_awareness_loop,
                    mail_awareness_loop,
                ) = register_awareness_loops(
                    app,
                    config=runtime_config,
                    database=runtime_database,
                    device_target_resolver=device_target_resolver,
                    device_command_gateway=device_command_gateway,
                    capability_models=capability_models,
                    timeline_store=timeline_store,
                    memory_store=memory_store,
                    perception_pipeline=perception_pipeline,
                    proactive_delivery=proactive_delivery,
                    cognitive_cycle=cognitive_cycle,
                )

    return app


app = create_app()
