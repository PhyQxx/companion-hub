"""PERE-03（docs/09 §8）组合根拆分——运行时装配段。

从 main.py 原样搬移：AuthService、device_tools 组装与计划 Runner 注入、
ChatService、鉴权后全部用户路由、Chat/小爱/语音 WebSocket、推送路由、
主动投递栈与感知循环注册。领域产物经 DomainAssembly 属性读取；运行期
产生的服务（chat/turn 协调/感知循环/安全栈）经 RuntimeAssembly 返回，
由 create_app 解包给 lifespan deps 灌入。路由顺序与装配条件保持不变。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta

from fastapi import FastAPI

from app.api import (
    create_admin_security_router,
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
    create_meetings_router,
    create_model_capability_router,
    create_pnkx_router,
    create_push_router,
    create_reviews_router,
    create_safety_router,
    create_sso_router,
    create_tasks_router,
    create_theme_router,
    create_todo_router,
    create_voice_websocket_router,
    create_workflows_router,
    create_xiaoai_websocket_router,
    load_sso_settings,
)
from app.api.mail import create_mail_router
from app.api.runs import create_runs_router
from app.auth import AuthService
from app.browser_awareness import BrowserAwarenessLoop
from app.calendar import CalendarCreateTool, CalendarSyncTool
from app.chat import (
    ChatService,
    CompositeRuntimeCapabilityProvider,
    RuntimeCapabilityProvider,
)
from app.cognition import PlanExecutionEvent, ToolActionRunner
from app.cognition.action_plan import PlanChangedEvent
from app.commute import CommuteCheckTool
from app.config import BrowserWorkflowConfig, DatabaseConfigStore, DesktopActionsConfig
from app.confirmation import DatabasePendingMutationStore
from app.contacts import ContactQueryTool, ContactSaveTool
from app.db import Database
from app.devices import DeviceTargetResolver, MqttPresenceBridge
from app.focus import FocusStartTool, FocusStatusTool, FocusStopTool
from app.home_assistant import (
    HomeAssistantProactiveEngine,
    HomeControlTool,
    HomeGetHistoryTool,
    HomeGetStateTool,
    SearchDevicesTool,
)
from app.home_scene import HomeSceneListTool, HomeSceneRunTool
from app.mail import (
    MailAttachmentStore,
    MailOutboxStore,
    MailSendTool,
    create_mail_tools,
)
from app.mail_awareness import MailAwarenessLoop
from app.output import ProactiveDeliveryService
from app.push import PushSubscriptionStore
from app.runs.speech_delivery import SqlSpeechDelivery
from app.runs.voice_sources import SqlVoiceSourceGuard
from app.runs.voice_turn import SqlVoiceTurnDelivery
from app.runtime import TurnCoordinator
from app.safety import SafetyActivityScheduler, SafetyAlertService
from app.screen_awareness import ScreenAwarenessLoop
from app.skills.writes import SkillWriteToolHandler
from app.tasks import ReminderCancelTool, ReminderCreateTool, ReminderListTool
from app.tools import DesktopNotifyTool, ToolExecutor, ToolHandler, ToolRegistry
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
from app.workflows import WorkflowRunTool, WorkflowSaveTool

from .domain import DomainAssembly
from .modules.conversation import build_conversation
from .proactive import register_awareness_loops, register_proactive_stack


@dataclass
class RuntimeAssembly:
    """运行时装配产物；create_app 解包后供 lifespan deps 灌入。"""

    chat_service: ChatService
    turn_coordinator: TurnCoordinator
    home_assistant_proactive: HomeAssistantProactiveEngine | None
    safety_alert_service: SafetyAlertService
    activity_scheduler: SafetyActivityScheduler
    mqtt_presence_bridge: MqttPresenceBridge | None
    screen_awareness_loop: ScreenAwarenessLoop | None
    browser_awareness_loop: BrowserAwarenessLoop | None
    mail_awareness_loop: MailAwarenessLoop | None


def assemble_runtime(
    app: FastAPI,
    domain: DomainAssembly,
    *,
    runtime_config: DatabaseConfigStore,
    runtime_database: Database,
    admin_token: str | None,
) -> RuntimeAssembly:
    """装配运行时（鉴权/对话/路由/WS/语音/推送/主动栈）；路由顺序不变。"""
    # 原组合根占位：由下方 register_proactive_stack / register_awareness_loops 赋值
    proactive_delivery: ProactiveDeliveryService | None = None
    safety_alert_service: SafetyAlertService | None = None
    activity_scheduler: SafetyActivityScheduler | None = None
    home_assistant_proactive: HomeAssistantProactiveEngine | None = None
    mqtt_presence_bridge: MqttPresenceBridge | None = None
    screen_awareness_loop: ScreenAwarenessLoop | None = None
    browser_awareness_loop: BrowserAwarenessLoop | None = None
    mail_awareness_loop: MailAwarenessLoop | None = None
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
            admin_token=admin_token,
            auth_service=auth_service,
        )
    )
    device_tools: list[ToolHandler] = []
    calendar_create_tool: CalendarCreateTool | None = None
    workflow_save_tool: WorkflowSaveTool | None = None
    capability_providers: list[RuntimeCapabilityProvider] = []
    device_target_resolver: DeviceTargetResolver | None = None
    device_command_gateway = None
    if domain.device_registry is not None:
        capability_providers.append(domain.device_registry)
        device_target_resolver = DeviceTargetResolver(domain.device_registry)
        admin_devices_router, devices_router = create_device_routers(
            domain.device_registry,
            admin_token=admin_token,
        )
        app.include_router(admin_devices_router)
        app.include_router(devices_router)
        if domain.device_command_store is not None:
            command_admin_router, device_ws_router, device_command_gateway = (
                create_device_command_routers(
                    domain.device_registry,
                    domain.device_command_store,
                    admin_token=admin_token,
                )
            )
            app.state.device_command_gateway = device_command_gateway
            app.include_router(command_admin_router)
            app.include_router(device_ws_router)
            if domain.capability_models is not None:
                screen_analyzer = CapabilityScreenAnalyzer(domain.capability_models)
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
    if domain.home_assistant_manager is not None:
        capability_providers.append(domain.home_assistant_manager)
        device_tools.append(SearchDevicesTool(domain.home_assistant_manager))
        device_tools.append(HomeGetStateTool(domain.home_assistant_manager))
        device_tools.append(HomeGetHistoryTool(domain.home_assistant_manager))
        device_tools.append(HomeControlTool(domain.home_assistant_manager))
    device_tools.append(
        ReadSensorsTool(
            ha_provider=domain.home_assistant_manager,
            mqtt_buffer=domain.mqtt_client.buffer if domain.mqtt_client is not None else None,
        )
    )
    default_timezone = os.getenv("ARIA_DEFAULT_TIMEZONE", "Asia/Shanghai")
    # FIX-01/01B 确认预览统一持久化：跨重启/跨 worker 存活
    pending_mutations = (
        DatabasePendingMutationStore(runtime_database) if runtime_database else None
    )
    if domain.task_store is not None:
        device_tools.append(ReminderCreateTool(domain.task_store, timezone_name=default_timezone))
        device_tools.append(ReminderListTool(domain.task_store, timezone_name=default_timezone))
        device_tools.append(ReminderCancelTool(domain.task_store, timezone_name=default_timezone))
    if domain.calendar_service is not None:
        calendar_create_tool = CalendarCreateTool(
            domain.calendar_service,
            timezone_name=default_timezone,
            drafts=pending_mutations,
        )
        device_tools.append(calendar_create_tool)
    device_tools.append(
        CalendarSyncTool(
            caldav_sync=domain.caldav_sync_service,
            google_sync=domain.google_calendar_sync_service,
        )
    )
    if domain.contact_store is not None:
        device_tools.append(ContactSaveTool(domain.contact_store))
        device_tools.append(ContactQueryTool(domain.contact_store))
    device_tools.append(CommuteCheckTool(domain.build_commute_service))
    if domain.workflow_service is not None:
        workflow_save_tool = WorkflowSaveTool(domain.workflow_service, drafts=pending_mutations)
        device_tools.append(workflow_save_tool)
        device_tools.append(WorkflowRunTool(domain.workflow_service))
    if domain.delegate_task_tool is not None:
        device_tools.append(domain.delegate_task_tool)
    if domain.propose_action_tool is not None:
        device_tools.append(domain.propose_action_tool)
    if domain.focus_service is not None:
        device_tools.extend(
            [
                FocusStartTool(domain.focus_service),
                FocusStopTool(domain.focus_service),
                FocusStatusTool(domain.focus_service),
            ]
        )
    if domain.home_scene_service is not None:
        device_tools.extend(
            [
                HomeSceneRunTool(domain.home_scene_service),
                HomeSceneListTool(domain.home_scene_service),
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
        domain.skill_store is not None
        and domain.skill_connections is not None
        and domain.skill_http_client is not None
    ):
        # 技能 S3 写动作执行器：仅经 Action Registry 计划—确认—执行链触发，
        # 不作为聊天工具挂载（chat 侧 _device_tool_ready 黑名单）。
        device_tools.append(
            SkillWriteToolHandler(
                domain.skill_store,
                domain.skill_connections,
                domain.skill_http_client,
            )
        )
    if domain.action_plan_service is not None:
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
            if domain.mcp_manager is not None:
                # MCP-D：唯一写调用入口，注册到计划执行器；聊天挂载恒关
                device_tools.append(McpToolCallTool(domain.mcp_manager))
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
        domain.action_plan_service.set_runner(
            ToolActionRunner(
                ToolExecutor(ToolRegistry(device_tools)),
                home_state_provider=domain.home_assistant_manager,
            )
        )
        if domain.plan_distiller is not None:
            # DIST：回放复用与计划执行同一套工具注册表与隐私闸门
            domain.plan_distiller.set_executor(ToolExecutor(ToolRegistry(device_tools)))
    capability_provider = (
        CompositeRuntimeCapabilityProvider(capability_providers)
        if capability_providers
        else None
    )
    runtime_chat_service = build_conversation(
        runtime_database, runtime_config, domain=domain,
        capability_provider=capability_provider, device_tools=device_tools,
    )
    app.state.auth_service = auth_service
    app.state.chat_service = runtime_chat_service
    app.include_router(create_auth_router(auth_service, admin_token=admin_token))
    # pnkx 统一登录（可选）：issuer/client/secret 与账号名单齐备才启用
    sso_settings = load_sso_settings()
    if sso_settings.enabled:
        sso_base_url = os.getenv("ARIA_SSO_BASE_URL", "").rstrip("/")
        app.include_router(
            create_sso_router(auth_service, sso_settings, base_url=sso_base_url)
        )
    app.include_router(create_chat_router(runtime_chat_service, auth_service))
    app.include_router(create_runs_router(runtime_chat_service, auth_service))
    for device_tool in device_tools:
        if isinstance(device_tool, MailSendTool):
            app.include_router(
                create_mail_router(device_tool, auth_service, attachments=mail_attachments)
            )
    if domain.avatar_store is not None and domain.persona_store is not None:
        app.include_router(
            create_avatar_router(domain.avatar_store, domain.persona_store, auth_service)
        )
    if domain.theme_store is not None:
        app.include_router(create_theme_router(domain.theme_store, auth_service))
    if domain.cognitive_store is not None:
        app.include_router(
            create_cognition_router(
                domain.cognitive_store,
                auth_service,
                domain.perception_store,
                domain.action_registry,
                domain.action_plan_service,
            )
        )
    if domain.task_store is not None:
        app.include_router(create_tasks_router(domain.task_store, auth_service))
        app.state.task_store = domain.task_store
        app.state.task_scheduler = domain.task_scheduler
        app.state.goal_reminder_scheduler = domain.goal_reminder_scheduler
    if domain.focus_service is not None:
        app.state.focus_service = domain.focus_service
        app.state.focus_scheduler = domain.focus_scheduler
    if domain.daily_brief_service is not None:
        app.include_router(create_briefs_router(domain.daily_brief_service, auth_service))
        app.state.daily_brief_service = domain.daily_brief_service
        app.state.daily_brief_scheduler = domain.daily_brief_scheduler
    if domain.daily_review_service is not None:
        app.include_router(create_reviews_router(domain.daily_review_service, auth_service))
        app.state.daily_review_service = domain.daily_review_service
        app.state.daily_review_scheduler = domain.daily_review_scheduler
    if domain.calendar_service is not None:
        app.include_router(
            create_calendar_router(
                domain.calendar_service,
                auth_service,
                calendar_create_tool,
                caldav_sync=domain.caldav_sync_service,
                google_sync=domain.google_calendar_sync_service,
                google_state_key=admin_token,
            )
        )
        app.state.calendar_service = domain.calendar_service
        app.state.caldav_sync_service = domain.caldav_sync_service
    if domain.meeting_service is not None:
        app.include_router(create_meetings_router(domain.meeting_service, auth_service))
        app.state.meeting_service = domain.meeting_service
    if domain.contact_store is not None:
        app.include_router(create_contacts_router(domain.contact_store, auth_service))
        if safety_alert_service is not None:
            app.include_router(create_safety_router(safety_alert_service, auth_service))
        app.state.contact_store = domain.contact_store
    if domain.home_scene_service is not None:
        app.include_router(create_home_scenes_router(domain.home_scene_service, auth_service))
        app.state.home_scene_service = domain.home_scene_service
    if domain.workflow_service is not None:
        app.include_router(
            create_workflows_router(domain.workflow_service, auth_service, workflow_save_tool)
        )
        app.state.workflow_service = domain.workflow_service
    if domain.todo_sync_service is not None:
        app.include_router(create_todo_router(domain.todo_sync_service, auth_service))
        app.state.todo_sync_service = domain.todo_sync_service
        app.state.todo_sync_scheduler = domain.todo_sync_scheduler
    if domain.pnkx_life_client is not None:
        app.include_router(
            create_pnkx_router(
                domain.pnkx_life_client,
                auth_service,
                writes_enabled=True,
            )
        )
        app.state.pnkx_life_client = domain.pnkx_life_client
    if domain.capability_models is not None:
        app.include_router(create_model_capability_router(domain.capability_models, auth_service))
    turn_coordinator = TurnCoordinator(runtime_database, runtime_chat_service)
    websocket_router, websocket_manager = create_chat_websocket_router(
        runtime_chat_service,
        auth_service,
        turn_coordinator=turn_coordinator,
        avatar_control_publisher=device_command_gateway,
    )
    app.state.chat_websocket_manager = websocket_manager
    if domain.action_plan_service is not None:

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

        domain.action_plan_service.set_execution_listener(push_plan_execution)
        domain.action_plan_service.set_change_listener(push_plan_changed)
    if device_command_gateway is not None:
        device_command_gateway.set_pet_message_handler(
            websocket_manager.submit_device_message
        )
    app.include_router(websocket_router)
    if domain.xiaoai_materializer is not None:
        xiaoai_router, xiaoai_manager = create_xiaoai_websocket_router(
            runtime_chat_service,
            credentials_provider=domain.xiaoai_materializer.credentials,
        )
        app.state.xiaoai_websocket_manager = xiaoai_manager
        app.include_router(xiaoai_router)
    voice_manager = None
    if runtime_config is not None:
        voice_source = ConfigVoiceSource(runtime_config)
        voice_guard = SqlVoiceSourceGuard(runtime_database)
        speech_delivery = SqlSpeechDelivery(
            runtime_database, runtime_config, voice_guard, voice_source
        )
        voice_router, voice_manager = create_voice_websocket_router(
            runtime_chat_service,
            auth_service,
            voice_source=voice_source,
            source_guard=voice_guard,
            speech_delivery=speech_delivery,
            voice_turn_delivery=SqlVoiceTurnDelivery(speech_delivery),
            turn_coordinator=turn_coordinator,
            avatar_control_publisher=device_command_gateway,
        )
        app.state.voice_websocket_manager = voice_manager
        app.include_router(
            create_admin_voice_router(
                voice_manager.latency_report,
                voice_manager.reset_latency_metrics,
                admin_token=admin_token,
            )
        )
        if device_command_gateway is not None:
            device_command_gateway.set_pet_audio_handler(
                voice_manager.stream_device_speech,
                source_guard=SqlVoiceSourceGuard(runtime_database),
            )
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
            deleg_worker=domain.deleg_worker,
            plan_completion_reporter=domain.plan_completion_reporter,
            activity_tracker=domain.activity_tracker,
            job_engine=domain.job_engine,
            admin_token=admin_token,
            cognitive_cycle=domain.cognitive_cycle,
            task_scheduler=domain.task_scheduler,
            goal_reminder_scheduler=domain.goal_reminder_scheduler,
            focus_scheduler=domain.focus_scheduler,
            daily_brief_scheduler=domain.daily_brief_scheduler,
            daily_review_scheduler=domain.daily_review_scheduler,
            timeline_store=domain.timeline_store,
            home_assistant_manager=domain.home_assistant_manager,
            mqtt_client=domain.mqtt_client,
            perception_pipeline=domain.perception_pipeline,
            self_check_scheduler=domain.self_check_scheduler,
        )
    if (
        runtime_database is not None
        and domain.timeline_store is not None
        and device_target_resolver is not None
        and device_command_gateway is not None
        and domain.capability_models is not None
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
            capability_models=domain.capability_models,
            timeline_store=domain.timeline_store,
            memory_store=domain.memory_store,
            perception_pipeline=domain.perception_pipeline,
            proactive_delivery=proactive_delivery,
            cognitive_cycle=domain.cognitive_cycle,
        )

    return RuntimeAssembly(
        chat_service=runtime_chat_service,
        turn_coordinator=turn_coordinator,
        home_assistant_proactive=home_assistant_proactive,
        safety_alert_service=safety_alert_service,
        activity_scheduler=activity_scheduler,
        mqtt_presence_bridge=mqtt_presence_bridge,
        screen_awareness_loop=screen_awareness_loop,
        browser_awareness_loop=browser_awareness_loop,
        mail_awareness_loop=mail_awareness_loop,
    )
