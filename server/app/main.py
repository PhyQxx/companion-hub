from __future__ import annotations

import os
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

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
from app.auth import AuthService
from app.browser_awareness import (
    BrowserAwarenessLoop,
)
from app.bus import EventPublisher
from app.calendar import (
    CalendarCreateTool,
    CalendarSyncTool,
)
from app.chat import ChatService, CompositeRuntimeCapabilityProvider, RuntimeCapabilityProvider
from app.cognition import (
    PlanExecutionEvent,
    ToolActionRunner,
)
from app.cognition.action_plan import PlanChangedEvent
from app.commute import CommuteCheckTool
from app.config import (
    BrowserWorkflowConfig,
    ConfigStore,
    DatabaseConfigStore,
    DesktopActionsConfig,
)
from app.confirmation import DatabasePendingMutationStore
from app.contacts import ContactQueryTool, ContactSaveTool
from app.db import Database, create_database
from app.devices import (
    DeviceTargetResolver,
    MqttPresenceBridge,
)
from app.focus import FocusStartTool, FocusStatusTool, FocusStopTool
from app.home_assistant import (
    HomeAssistantProactiveEngine,
    HomeControlTool,
    HomeGetHistoryTool,
    HomeGetStateTool,
    SearchDevicesTool,
)
from app.home_scene import (
    HomeSceneListTool,
    HomeSceneRunTool,
)
from app.integrations.mcp.chat_tools import McpChatToolProvider
from app.mail import (
    MailAttachmentStore,
    MailOutboxStore,
    MailSendTool,
    create_mail_tools,
)
from app.mail_awareness import MailAwarenessLoop
from app.observability import configure_logging
from app.output import ProactiveDeliveryService
from app.push import PushSubscriptionStore
from app.runtime import TurnCoordinator
from app.safety import SafetyActivityScheduler, SafetyAlertService
from app.screen_awareness import (
    ScreenAwarenessLoop,
)
from app.skills.writes import SkillWriteToolHandler
from app.tasks import (
    ReminderCancelTool,
    ReminderCreateTool,
    ReminderListTool,
)
from app.tools import (
    DesktopNotifyTool,
    ToolExecutor,
    ToolHandler,
    ToolRegistry,
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
from app.tools.mcp_actions import McpToolCallTool
from app.tools.screen import CapabilityScreenAnalyzer, CaptureScreenTool
from app.tools.sensors import ReadSensorsTool
from app.voice import ConfigVoiceSource
from app.wiring.admin_routers import register_admin_routers
from app.wiring.domain import assemble_domain
from app.wiring.frontend import register_frontend
from app.wiring.lifespan import LifespanDeps, build_lifespan
from app.wiring.proactive import register_awareness_loops, register_proactive_stack
from app.workflows import (
    WorkflowRunTool,
    WorkflowSaveTool,
)


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

    # 生命周期内被运行时段赋值、deps 灌入读取的占位
    runtime_chat_service: ChatService | None = None
    home_assistant_proactive: HomeAssistantProactiveEngine | None = None
    screen_awareness_loop: ScreenAwarenessLoop | None = None
    browser_awareness_loop: BrowserAwarenessLoop | None = None
    mail_awareness_loop: MailAwarenessLoop | None = None
    safety_alert_service: SafetyAlertService | None = None
    activity_scheduler: SafetyActivityScheduler | None = None
    proactive_delivery: ProactiveDeliveryService | None = None
    mqtt_presence_bridge: MqttPresenceBridge | None = None
    turn_coordinator: TurnCoordinator | None = None

    async def test_home_assistant_proactive() -> bool:
        if home_assistant_proactive is None:
            return False
        return await home_assistant_proactive.send_test_message() is not None

    # PERE-03 第五批：领域装配段搬至 wiring/domain.py（行为不变；顺带删除
    # 从未被引用的 deliver_task_reminder/deliver_goal_reminder 死代码，
    # 活版本在 wiring/proactive.py）
    domain = assemble_domain(
        runtime_database,
        runtime_config,
        event_publisher=event_publisher,
        run_dispatcher=run_dispatcher,
        watch_config=watch_config,
    )
    # 过渡解包：运行时段（第六批搬移前）仍以局部名读取领域产物
    config_watcher = domain.config_watcher
    capability_models = domain.capability_models
    home_assistant_manager = domain.home_assistant_manager
    xiaoai_materializer = domain.xiaoai_materializer
    mcp_manager = domain.mcp_manager
    worker = domain.worker
    mqtt_client = domain.mqtt_client
    activity_tracker = domain.activity_tracker
    skill_store = domain.skill_store
    skill_connections = domain.skill_connections
    skill_credentials = domain.skill_credentials
    skill_http_client = domain.skill_http_client
    skill_tool_provider = domain.skill_tool_provider
    skill_generator = domain.skill_generator
    skill_draft_assistant = domain.skill_draft_assistant
    persona_store = domain.persona_store
    memory_store = domain.memory_store
    timeline_store = domain.timeline_store
    device_registry = domain.device_registry
    device_command_store = domain.device_command_store
    job_engine = domain.job_engine
    asset_store = domain.asset_store
    avatar_store = domain.avatar_store
    avatar_upload_root = domain.avatar_upload_root
    avatar_importer = domain.avatar_importer
    theme_store = domain.theme_store
    cognitive_store = domain.cognitive_store
    action_registry = domain.action_registry
    action_plan_service = domain.action_plan_service
    workflow_service = domain.workflow_service
    workflow_draft_store = domain.workflow_draft_store
    plan_distiller = domain.plan_distiller
    plan_completion_reporter = domain.plan_completion_reporter
    propose_action_tool = domain.propose_action_tool
    cognitive_cycle = domain.cognitive_cycle
    perception_store = domain.perception_store
    perception_pipeline = domain.perception_pipeline
    task_store = domain.task_store
    task_scheduler = domain.task_scheduler
    goal_tracker = domain.goal_tracker
    goal_reminder_scheduler = domain.goal_reminder_scheduler
    focus_service = domain.focus_service
    focus_scheduler = domain.focus_scheduler
    home_scene_service = domain.home_scene_service
    contact_store = domain.contact_store
    calendar_service = domain.calendar_service
    meeting_service = domain.meeting_service
    daily_brief_service = domain.daily_brief_service
    daily_brief_scheduler = domain.daily_brief_scheduler
    daily_review_service = domain.daily_review_service
    daily_review_scheduler = domain.daily_review_scheduler
    todo_sync_service = domain.todo_sync_service
    todo_sync_scheduler = domain.todo_sync_scheduler
    pnkx_life_client = domain.pnkx_life_client
    caldav_sync_service = domain.caldav_sync_service
    caldav_sync_scheduler = domain.caldav_sync_scheduler
    google_calendar_sync_service = domain.google_calendar_sync_service
    google_calendar_sync_scheduler = domain.google_calendar_sync_scheduler
    web_fetch_tool = domain.web_fetch_tool
    deleg_worker = domain.deleg_worker
    delegate_task_tool = domain.delegate_task_tool
    history_recall = domain.history_recall
    memory_extractor = domain.memory_extractor
    build_commute_service = domain.build_commute_service

    # PERE-03：lifespan 启停序列搬至 wiring/lifespan.py；
    # deps 在 return app 前统一灌入最终值，启动时按属性读取。
    deps = LifespanDeps()
    lifespan = build_lifespan(deps)

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
                device_tools.append(ReminderListTool(task_store, timezone_name=default_timezone))
                device_tools.append(ReminderCancelTool(task_store, timezone_name=default_timezone))
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
                action_registry=action_registry,
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

    # PERE-03：lifespan 依赖快照灌入（全部装配已完成，此处为最终值）
    deps.runtime_config = runtime_config
    deps.runtime_database = runtime_database
    deps.owns_database = owns_database
    deps.xiaoai_materializer = xiaoai_materializer
    deps.persona_store = persona_store
    deps.memory_store = memory_store
    deps.home_assistant_manager = home_assistant_manager
    deps.mqtt_client = mqtt_client
    deps.avatar_store = avatar_store
    deps.theme_store = theme_store
    deps.runtime_chat_service = runtime_chat_service
    deps.turn_coordinator = turn_coordinator
    deps.config_watcher = config_watcher
    deps.worker = worker
    deps.deleg_worker = deleg_worker
    deps.screen_awareness_loop = screen_awareness_loop
    deps.browser_awareness_loop = browser_awareness_loop
    deps.mail_awareness_loop = mail_awareness_loop
    deps.mcp_manager = mcp_manager
    deps.safety_alert_service = safety_alert_service
    deps.activity_scheduler = activity_scheduler
    deps.task_scheduler = task_scheduler
    deps.goal_reminder_scheduler = goal_reminder_scheduler
    deps.focus_scheduler = focus_scheduler
    deps.daily_brief_scheduler = daily_brief_scheduler
    deps.daily_review_scheduler = daily_review_scheduler
    deps.todo_sync_scheduler = todo_sync_scheduler
    deps.caldav_sync_scheduler = caldav_sync_scheduler
    deps.google_calendar_sync_scheduler = google_calendar_sync_scheduler
    deps.skill_store = skill_store
    deps.action_registry = action_registry
    deps.skill_http_client = skill_http_client
    deps.skill_audit_scheduler = domain.skill_audit_scheduler
    deps.pnkx_life_client = pnkx_life_client
    deps.home_assistant_proactive = home_assistant_proactive
    deps.mqtt_presence_bridge = mqtt_presence_bridge
    deps.perception_pipeline = perception_pipeline

    return app


app = create_app()
