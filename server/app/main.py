from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI

from app import __version__
from app.adapters import AdapterRegistry
from app.adapters.builtin import create_builtin_registry
from app.browser_awareness import (
    BrowserAwarenessLoop,
)
from app.bus import EventPublisher
from app.chat import ChatService
from app.config import (
    ConfigStore,
    DatabaseConfigStore,
)
from app.db import Database, create_database
from app.devices import (
    MqttPresenceBridge,
)
from app.home_assistant import (
    HomeAssistantProactiveEngine,
)
from app.mail_awareness import MailAwarenessLoop
from app.observability import configure_logging
from app.runtime import TurnCoordinator
from app.safety import SafetyActivityScheduler, SafetyAlertService
from app.screen_awareness import (
    ScreenAwarenessLoop,
)
from app.wiring.admin_routers import register_admin_data_routers, register_admin_routers
from app.wiring.domain import assemble_domain
from app.wiring.frontend import register_frontend
from app.wiring.lifespan import LifespanDeps, build_lifespan
from app.wiring.runtime import RuntimeAssembly, assemble_runtime


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
    # 组合根后续段（app.state/前端/Admin 注册/deps 灌入）以局部名读取领域产物
    config_watcher = domain.config_watcher
    capability_models = domain.capability_models
    home_assistant_manager = domain.home_assistant_manager
    xiaoai_materializer = domain.xiaoai_materializer
    mcp_manager = domain.mcp_manager
    worker = domain.worker
    mqtt_client = domain.mqtt_client
    skill_store = domain.skill_store
    skill_connections = domain.skill_connections
    skill_credentials = domain.skill_credentials
    skill_http_client = domain.skill_http_client
    skill_tool_provider = domain.skill_tool_provider
    skill_generator = domain.skill_generator
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
    action_registry = domain.action_registry
    action_plan_service = domain.action_plan_service
    perception_store = domain.perception_store
    perception_pipeline = domain.perception_pipeline
    task_scheduler = domain.task_scheduler
    goal_reminder_scheduler = domain.goal_reminder_scheduler
    focus_scheduler = domain.focus_scheduler
    daily_brief_scheduler = domain.daily_brief_scheduler
    daily_review_scheduler = domain.daily_review_scheduler
    todo_sync_scheduler = domain.todo_sync_scheduler
    pnkx_life_client = domain.pnkx_life_client
    caldav_sync_scheduler = domain.caldav_sync_scheduler
    google_calendar_sync_scheduler = domain.google_calendar_sync_scheduler
    deleg_worker = domain.deleg_worker
    history_recall = domain.history_recall

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
    # PERE-03：Admin 前置路由搬至 wiring/admin_routers.py，Admin 数据路由与
    # 运行时装配段搬至 register_admin_data_routers / wiring/runtime.py
    runtime_admin_token: str | None = None
    runtime: RuntimeAssembly | None = None
    if isinstance(runtime_config, DatabaseConfigStore):
        runtime_admin_token = register_admin_routers(
            app,
            config=runtime_config,
            database=runtime_database,
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
            register_admin_data_routers(
                app,
                database=runtime_database,
                admin_token=runtime_admin_token,
                domain=domain,
            )
            runtime = assemble_runtime(
                app,
                domain,
                runtime_config=runtime_config,
                runtime_database=runtime_database,
                admin_token=runtime_admin_token,
            )

    if runtime is not None:
        # 解包回组合根局部名：闭包（ha 主动测试）与 deps 灌入按原语义读取
        runtime_chat_service = runtime.chat_service
        turn_coordinator = runtime.turn_coordinator
        home_assistant_proactive = runtime.home_assistant_proactive
        screen_awareness_loop = runtime.screen_awareness_loop
        browser_awareness_loop = runtime.browser_awareness_loop
        mail_awareness_loop = runtime.mail_awareness_loop
        safety_alert_service = runtime.safety_alert_service
        activity_scheduler = runtime.activity_scheduler
        mqtt_presence_bridge = runtime.mqtt_presence_bridge

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
    deps.plan_distiller = domain.plan_distiller
    deps.plan_completion_reporter = domain.plan_completion_reporter
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
    deps.self_check_scheduler = domain.self_check_scheduler

    return app


app = create_app()
