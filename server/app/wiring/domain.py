"""PERE-03（docs/09 §8）组合根拆分——领域装配段。

从 main.py 原样搬移：store/服务/调度器/对话工具的构造与互连（人格、
记忆、时间线、设备、任务、承诺、专注、场景、简报、回顾、日历、会议、
pnkx 同步、技能、DELEG、CalDAV/Google 镜像、MQTT、分发 worker）。
跨段晚绑定（观察口闭包引用后置的 home_scene_service、commute 服务
引用后置的 calendar_service）在同一函数作用域内保持原语义；原先经
占位变量晚绑定的 test_home_assistant_proactive 仍留在 create_app。
搬移时删除了 deliver_task_reminder/deliver_goal_reminder 死代码——
活版本在 wiring/proactive.py，main.py 内的副本从未被引用。

装配产物经 DomainAssembly 容器交给运行时段与 lifespan deps 灌入。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from app.appearance import ThemeStore
from app.avatar import AvatarAssetImporter, AvatarStore
from app.bus import DispatcherWorker, EventPublisher, LocalEventPublisher
from app.calendar import (
    CalDavSyncScheduler,
    CalDavSyncService,
    CalendarService,
    CalendarStore,
    GoogleCalendarSyncScheduler,
    GoogleCalendarSyncService,
)
from app.cognition import (
    ActionPlanService,
    ActionRegistry,
    AttentionEngine,
    CognitiveCycle,
    CognitiveStore,
    GoalTracker,
    PlanCompletionReporter,
    ProposeActionTool,
    RouterDeliberator,
    RuleBasedDeliberator,
    SemanticEvent,
    WorldStateBuilder,
    build_builtin_action_registry,
)
from app.commute import CommuteService
from app.config import ConfigStore, ConfigWatcher, DatabaseConfigStore
from app.contacts import ContactStore
from app.db import Database
from app.devices import DeviceCommandStore, DeviceRegistry
from app.devices.mqtt_client import MqttDeviceClient, MqttTelemetryBuffer
from app.focus import FocusScheduler, FocusService
from app.home_assistant import HomeAssistantManager
from app.home_scene import HomeSceneService, HomeSceneStore
from app.integrations.mcp import McpManager
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
from app.meetings import LlmMeetingSummarizer, MeetingService, MeetingStore
from app.memory import (
    LlmMemoryExtractor,
    MemoryExtractor,
    MemoryRetriever,
    MemoryStore,
)
from app.model_capabilities import CapabilityModelService
from app.observability import apply_observability
from app.perception import PerceptionPipeline, PerceptionStore, ProactivePolicy
from app.perception.pipeline import EventObserver
from app.persona import PersonaStore
from app.pnkx import PnkxLifeClient
from app.safety import ActivityTracker
from app.skills.audit import SkillAuditScheduler
from app.skills.connections import SkillConnectionStore, SkillHttpClient
from app.skills.credentials import SkillCredentialStore
from app.skills.drafts import SkillDraftAssistant
from app.skills.generator import SkillDraftGenerator
from app.skills.runtime import SkillToolProvider
from app.skills.store import SkillStore
from app.tasks import TaskScheduler, TaskStore
from app.tasks.brief import BriefCommute, BriefWeather, DailyBriefService
from app.tasks.brief_scheduler import DailyBriefScheduler
from app.tasks.goal_scheduler import GoalReminderScheduler
from app.tasks.review import DailyReviewService
from app.tasks.review_scheduler import DailyReviewScheduler
from app.timeline import HistoryRecallService, TimelineStore
from app.todo import PnkxTodoClient, TodoSyncService
from app.todo.sync_scheduler import TodoSyncScheduler
from app.tools import FetchWebpageTool, build_query_tool_runtime
from app.tools.location import resolve_location
from app.workflows import WorkflowService, WorkflowStore
from app.workflows.drafts import PlanDistiller, WorkflowDraftStore
from app.xiaoai_config import XiaoAiConfigMaterializer


def _parse_brief_time(raw: str) -> dt_time:
    hour, minute = raw.split(":", 1)
    return dt_time(int(hour), int(minute))


def _parse_review_time(raw: str) -> dt_time:
    hour, minute = raw.split(":", 1)
    return dt_time(int(hour), int(minute))


@dataclass
class DomainAssembly:
    """领域装配产物；运行时段、前端/Admin 注册与 lifespan deps 读取。"""

    config_watcher: ConfigWatcher | None
    capability_models: CapabilityModelService | None
    home_assistant_manager: HomeAssistantManager | None
    xiaoai_materializer: XiaoAiConfigMaterializer | None
    mcp_manager: McpManager | None
    worker: DispatcherWorker | None
    mqtt_client: MqttDeviceClient | None
    activity_tracker: ActivityTracker
    skill_store: SkillStore | None
    skill_connections: SkillConnectionStore | None
    skill_credentials: SkillCredentialStore | None
    skill_http_client: SkillHttpClient | None
    skill_tool_provider: SkillToolProvider | None
    skill_generator: SkillDraftGenerator | None
    skill_draft_assistant: SkillDraftAssistant | None
    persona_store: PersonaStore | None
    memory_store: MemoryStore | None
    timeline_store: TimelineStore | None
    device_registry: DeviceRegistry | None
    device_command_store: DeviceCommandStore | None
    job_engine: JobEngine | None
    asset_store: AssetStore | None
    avatar_store: AvatarStore | None
    avatar_upload_root: Path
    avatar_importer: AvatarAssetImporter | None
    theme_store: ThemeStore | None
    cognitive_store: CognitiveStore | None
    action_registry: ActionRegistry
    action_plan_service: ActionPlanService | None
    workflow_service: WorkflowService | None
    workflow_draft_store: WorkflowDraftStore | None
    plan_distiller: PlanDistiller | None
    plan_completion_reporter: PlanCompletionReporter | None
    propose_action_tool: ProposeActionTool | None
    cognitive_cycle: CognitiveCycle | None
    perception_store: PerceptionStore | None
    perception_pipeline: PerceptionPipeline | None
    task_store: TaskStore | None
    task_scheduler: TaskScheduler | None
    goal_tracker: GoalTracker | None
    goal_reminder_scheduler: GoalReminderScheduler | None
    focus_service: FocusService | None
    focus_scheduler: FocusScheduler | None
    home_scene_service: HomeSceneService | None
    contact_store: ContactStore | None
    calendar_service: CalendarService | None
    meeting_service: MeetingService | None
    daily_brief_service: DailyBriefService | None
    daily_brief_scheduler: DailyBriefScheduler | None
    daily_review_service: DailyReviewService | None
    daily_review_scheduler: DailyReviewScheduler | None
    todo_sync_service: TodoSyncService | None
    todo_sync_scheduler: TodoSyncScheduler | None
    pnkx_life_client: PnkxLifeClient | None
    caldav_sync_service: CalDavSyncService | None
    caldav_sync_scheduler: CalDavSyncScheduler | None
    google_calendar_sync_service: GoogleCalendarSyncService | None
    google_calendar_sync_scheduler: GoogleCalendarSyncScheduler | None
    web_fetch_tool: FetchWebpageTool | None
    deleg_worker: DelegatedJobWorker | None
    delegate_task_tool: DelegateTaskTool | None
    history_recall: HistoryRecallService | None
    memory_extractor: MemoryExtractor | None
    build_commute_service: Callable[[], CommuteService | None]
    skill_audit_scheduler: SkillAuditScheduler | None


def assemble_domain(
    runtime_database: Database | None,
    runtime_config: ConfigStore | DatabaseConfigStore | None,
    *,
    event_publisher: EventPublisher | None,
    run_dispatcher: bool | None,
    watch_config: bool | None,
) -> DomainAssembly:
    """装配领域对象；返回组合根后续段需要的全部产物。"""
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
    activity_tracker = ActivityTracker()
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
                lambda config: (
                    build_router(config, _distill_secrets) if runtime_config is not None else None
                )
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
        PlanCompletionReporter(action_plan_service) if action_plan_service is not None else None
    )
    if action_plan_service is not None and plan_completion_reporter is not None:
        action_plan_service.add_completion_callback(plan_completion_reporter.on_plan_completed)
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
    task_scheduler: TaskScheduler | None = None
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
    calendar_store = CalendarStore(runtime_database) if runtime_database is not None else None

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
        if job_engine is not None and runtime_config is not None and web_fetch_tool is not None
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

    skill_audit_scheduler = (
        SkillAuditScheduler(skill_store, skill_connections) if skill_store is not None else None
    )
    return DomainAssembly(
        config_watcher=config_watcher,
        capability_models=capability_models,
        home_assistant_manager=home_assistant_manager,
        xiaoai_materializer=xiaoai_materializer,
        mcp_manager=mcp_manager,
        worker=worker,
        mqtt_client=mqtt_client,
        activity_tracker=activity_tracker,
        skill_store=skill_store,
        skill_connections=skill_connections,
        skill_credentials=skill_credentials,
        skill_http_client=skill_http_client,
        skill_tool_provider=skill_tool_provider,
        skill_generator=skill_generator,
        skill_draft_assistant=skill_draft_assistant,
        persona_store=persona_store,
        memory_store=memory_store,
        timeline_store=timeline_store,
        device_registry=device_registry,
        device_command_store=device_command_store,
        job_engine=job_engine,
        asset_store=asset_store,
        avatar_store=avatar_store,
        avatar_upload_root=avatar_upload_root,
        avatar_importer=avatar_importer,
        theme_store=theme_store,
        cognitive_store=cognitive_store,
        action_registry=action_registry,
        action_plan_service=action_plan_service,
        workflow_service=workflow_service,
        workflow_draft_store=workflow_draft_store,
        plan_distiller=plan_distiller,
        plan_completion_reporter=plan_completion_reporter,
        propose_action_tool=propose_action_tool,
        cognitive_cycle=cognitive_cycle,
        perception_store=perception_store,
        perception_pipeline=perception_pipeline,
        task_store=task_store,
        task_scheduler=task_scheduler,
        goal_tracker=goal_tracker,
        goal_reminder_scheduler=goal_reminder_scheduler,
        focus_service=focus_service,
        focus_scheduler=focus_scheduler,
        home_scene_service=home_scene_service,
        contact_store=contact_store,
        calendar_service=calendar_service,
        meeting_service=meeting_service,
        daily_brief_service=daily_brief_service,
        daily_brief_scheduler=daily_brief_scheduler,
        daily_review_service=daily_review_service,
        daily_review_scheduler=daily_review_scheduler,
        todo_sync_service=todo_sync_service,
        todo_sync_scheduler=todo_sync_scheduler,
        pnkx_life_client=pnkx_life_client,
        caldav_sync_service=caldav_sync_service,
        caldav_sync_scheduler=caldav_sync_scheduler,
        google_calendar_sync_service=google_calendar_sync_service,
        google_calendar_sync_scheduler=google_calendar_sync_scheduler,
        web_fetch_tool=web_fetch_tool,
        deleg_worker=deleg_worker,
        delegate_task_tool=delegate_task_tool,
        history_recall=history_recall,
        memory_extractor=memory_extractor,
        build_commute_service=build_commute_service,
        skill_audit_scheduler=skill_audit_scheduler,
    )
