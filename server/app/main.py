from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.adapters import AdapterRegistry
from app.adapters.builtin import create_builtin_registry
from app.api import (
    create_admin_avatar_router,
    create_admin_config_router,
    create_admin_dashboard_router,
    create_admin_jobs_router,
    create_admin_memory_router,
    create_admin_persona_router,
    create_admin_screen_awareness_router,
    create_admin_security_router,
    create_admin_theme_router,
    create_admin_timeline_router,
    create_auth_router,
    create_avatar_router,
    create_chat_router,
    create_chat_websocket_router,
    create_cognition_router,
    create_deletion_ledger_router,
    create_device_command_routers,
    create_device_routers,
    create_logs_stream_router,
    create_model_capability_router,
    create_theme_router,
    create_voice_websocket_router,
)
from app.api.admin_config import set_runtime_admin_token
from app.api.events import create_event_router
from app.appearance import ThemeStore
from app.auth import AuthService
from app.avatar import AvatarAssetImporter, AvatarStore
from app.bus import DispatcherWorker, EventPublisher, LocalEventPublisher
from app.chat import ChatService, CompositeRuntimeCapabilityProvider, RuntimeCapabilityProvider
from app.cognition import (
    AttentionEngine,
    CognitiveCycle,
    CognitiveStore,
    RouterDeliberator,
    RuleBasedDeliberator,
    WorldStateBuilder,
)
from app.config import ConfigStore, ConfigWatcher, DatabaseConfigStore
from app.db import Database, create_database
from app.devices import (
    DeviceCommandStore,
    DeviceRegistry,
    DeviceTargetResolver,
    MqttPresenceBridge,
)
from app.devices.mqtt_client import MqttDeviceClient, MqttTelemetryBuffer
from app.home_assistant import (
    HomeAssistantManager,
    HomeAssistantProactiveEngine,
    HomeControlTool,
    HomeGetHistoryTool,
    HomeGetStateTool,
)
from app.jobs import AssetStore, JobEngine
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
from app.persona import PersonaStore
from app.runtime import TurnCoordinator
from app.screen_awareness import (
    ScreenAwarenessAnalyzer,
    ScreenAwarenessGateway,
    ScreenAwarenessLoop,
    ScreenAwarenessResolver,
)
from app.timeline import HistoryRecallService, TimelineStore
from app.tools import ToolHandler
from app.tools.browser import InspectWebpageTool
from app.tools.screen import CapabilityScreenAnalyzer, CaptureScreenTool
from app.tools.sensors import ReadSensorsTool
from app.voice import ConfigVoiceSource


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
    dispatcher_enabled = run_dispatcher
    if dispatcher_enabled is None:
        dispatcher_enabled = os.getenv("ARIA_RUN_DISPATCHER", "false").lower() == "true"
    worker = None
    runtime_chat_service: ChatService | None = None
    home_assistant_proactive: HomeAssistantProactiveEngine | None = None
    screen_awareness_loop: ScreenAwarenessLoop | None = None
    proactive_delivery: ProactiveDeliveryService | None = None
    mqtt_presence_bridge: MqttPresenceBridge | None = None

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
        AvatarAssetImporter(avatar_upload_root, avatar_store)
        if avatar_store is not None
        else None
    )
    theme_store = ThemeStore(runtime_database) if runtime_database is not None else None
    cognitive_store = CognitiveStore(runtime_database) if runtime_database is not None else None
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

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if runtime_config is not None:
            await runtime_config.load()
            apply_observability(runtime_config.current.config)
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
        if config_watcher is not None:
            await config_watcher.start()
        if worker is not None:
            await worker.start()
        if screen_awareness_loop is not None:
            screen_awareness_loop.start()
        try:
            yield
        finally:
            if screen_awareness_loop is not None:
                await screen_awareness_loop.stop()
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
    chat_dist_assets = chat_dist / "assets"
    if chat_dist_assets.is_dir():
        app.mount("/chat/assets", StaticFiles(directory=chat_dist_assets), name="chat-assets")
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

    @app.get("/chat", include_in_schema=False)
    async def chat_entry() -> FileResponse:
        if (chat_dist / "index.html").is_file():
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
        app.include_router(
            create_admin_config_router(
                runtime_config,
                admin_token=runtime_admin_token,
                on_publish=(
                    home_assistant_manager.reconfigure
                    if home_assistant_manager is not None
                    else None
                ),
                on_proactive_test=test_home_assistant_proactive,
            )
        )
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
        if runtime_database is not None:
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
            app.include_router(
                create_admin_screen_awareness_router(
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
            auth_service = AuthService(runtime_database)
            app.include_router(
                create_admin_security_router(
                    admin_token=runtime_admin_token,
                    auth_service=auth_service,
                )
            )
            device_tools: list[ToolHandler] = []
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
                device_tools.append(HomeGetStateTool(home_assistant_manager))
                device_tools.append(HomeGetHistoryTool(home_assistant_manager))
                device_tools.append(HomeControlTool(home_assistant_manager))
            device_tools.append(
                ReadSensorsTool(
                    ha_provider=home_assistant_manager,
                    mqtt_buffer=mqtt_client.buffer if mqtt_client is not None else None,
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
                cognitive_cycle=cognitive_cycle,
                avatar_store=avatar_store,
            )
            app.state.auth_service = auth_service
            app.state.chat_service = runtime_chat_service
            app.include_router(create_auth_router(auth_service, admin_token=runtime_admin_token))
            app.include_router(create_chat_router(runtime_chat_service, auth_service))
            if avatar_store is not None and persona_store is not None:
                app.include_router(create_avatar_router(avatar_store, persona_store, auth_service))
            if theme_store is not None:
                app.include_router(create_theme_router(theme_store, auth_service))
            if cognitive_store is not None:
                app.include_router(
                    create_cognition_router(cognitive_store, auth_service, perception_store)
                )
            if capability_models is not None:
                app.include_router(create_model_capability_router(capability_models, auth_service))
            turn_coordinator = TurnCoordinator(runtime_database, runtime_chat_service)
            websocket_router, websocket_manager = create_chat_websocket_router(
                runtime_chat_service, auth_service, turn_coordinator=turn_coordinator
            )
            app.state.chat_websocket_manager = websocket_manager
            app.include_router(websocket_router)
            voice_manager = None
            if runtime_config is not None:
                voice_router, voice_manager = create_voice_websocket_router(
                    runtime_chat_service,
                    auth_service,
                    voice_source=ConfigVoiceSource(runtime_config),
                    turn_coordinator=turn_coordinator,
                )
                app.state.voice_websocket_manager = voice_manager
                app.include_router(voice_router)
            if home_assistant_manager is not None:
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
                )
                app.state.proactive_delivery_service = proactive_delivery

                home_assistant_proactive = HomeAssistantProactiveEngine(
                    runtime_database,
                    runtime_config,
                    home_assistant_manager.get_state,
                    proactive_delivery.deliver,
                    cognitive_cycle=cognitive_cycle,
                    perception_pipeline=perception_pipeline,
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

    return app


app = create_app()
