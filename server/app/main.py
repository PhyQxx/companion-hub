from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.adapters import AdapterRegistry
from app.adapters.builtin import create_builtin_registry
from app.api import (
    create_admin_config_router,
    create_admin_persona_router,
    create_auth_router,
    create_chat_router,
    create_chat_websocket_router,
)
from app.api.events import create_event_router
from app.auth import AuthService
from app.bus import DispatcherWorker, EventPublisher, LocalEventPublisher
from app.chat import ChatService
from app.config import ConfigStore, ConfigWatcher, DatabaseConfigStore
from app.db import Database, create_database
from app.persona import PersonaStore


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
        ConfigWatcher(runtime_config)
        if isinstance(runtime_config, ConfigStore) and config_watch_enabled
        else None
    )
    dispatcher_enabled = run_dispatcher
    if dispatcher_enabled is None:
        dispatcher_enabled = os.getenv("ARIA_RUN_DISPATCHER", "false").lower() == "true"
    worker = None
    runtime_chat_service: ChatService | None = None
    persona_store = PersonaStore(runtime_database) if runtime_database is not None else None
    if dispatcher_enabled and runtime_database is not None:
        publisher = event_publisher or LocalEventPublisher(runtime_database)
        worker = DispatcherWorker(runtime_database.sessions, publisher)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if runtime_config is not None:
            await runtime_config.load()
        if persona_store is not None:
            await persona_store.load()
        if runtime_chat_service is not None:
            await runtime_chat_service.recover_incomplete_turns()
        if config_watcher is not None:
            await config_watcher.start()
        if worker is not None:
            await worker.start()
        try:
            yield
        finally:
            if worker is not None:
                await worker.stop()
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

    admin_root = Path(__file__).parent / "admin"
    app.mount("/admin/assets", StaticFiles(directory=admin_root), name="admin-assets")
    chat_root = Path(__file__).parent / "chat_ui"
    app.mount("/chat/assets", StaticFiles(directory=chat_root), name="chat-assets")

    @app.get("/admin/models", include_in_schema=False)
    async def model_admin() -> FileResponse:
        return FileResponse(admin_root / "models.html")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/memory", include_in_schema=False)
    @app.get("/admin/devices", include_in_schema=False)
    @app.get("/admin/logs", include_in_schema=False)
    @app.get("/admin/privacy", include_in_schema=False)
    @app.get("/admin/settings", include_in_schema=False)
    async def admin_module() -> FileResponse:
        return FileResponse(admin_root / "module.html")

    @app.get("/admin/personas", include_in_schema=False)
    async def persona_admin() -> FileResponse:
        return FileResponse(admin_root / "personas.html")

    @app.get("/chat", include_in_schema=False)
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
            result["persona"] = {
                "version": persona_store.current.version,
                "content_hash": persona_store.current.content_hash,
                "name": persona_store.current.persona.name,
            }
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

    @app.get("/api/v1/meta/adapters", tags=["system"])
    async def adapters() -> dict[str, object]:
        return {
            "adapters": [
                manifest.model_dump(mode="json")
                for manifest in runtime_adapters.list_manifests()
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
        app.include_router(
            create_admin_config_router(
                runtime_config,
                admin_token=runtime_admin_token,
            )
        )
        if persona_store is not None:
            app.include_router(
                create_admin_persona_router(
                    persona_store,
                    admin_token=runtime_admin_token,
                )
            )
        if runtime_database is not None:
            auth_service = AuthService(runtime_database)
            runtime_chat_service = ChatService(
                runtime_database,
                runtime_config,
                persona_store=persona_store,
            )
            app.state.auth_service = auth_service
            app.state.chat_service = runtime_chat_service
            app.include_router(
                create_auth_router(auth_service, admin_token=runtime_admin_token)
            )
            app.include_router(create_chat_router(runtime_chat_service, auth_service))
            websocket_router, websocket_manager = create_chat_websocket_router(
                runtime_chat_service, auth_service
            )
            app.state.chat_websocket_manager = websocket_manager
            app.include_router(websocket_router)

    return app


app = create_app()
