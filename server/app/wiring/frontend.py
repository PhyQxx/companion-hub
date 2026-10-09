"""PERE-03（docs/09 §8）组合根拆分——前端静态资源与系统端点。

从 main.py 原样搬移：静态挂载（Admin SPA/Chat PWA/桌宠/形象资源/
Live2D 运行时）、SPA 回退路由、/healthz 与 /api/v1/meta/* 系统端点、
开发事件端点。只做装配搬移，行为与路由顺序保持不变。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.adapters import AdapterRegistry
from app.api.events import create_event_router
from app.avatar import AvatarStore
from app.bus import DispatcherWorker
from app.config import ConfigStore, DatabaseConfigStore
from app.db import Database
from app.home_assistant import HomeAssistantManager
from app.persona import PersonaStore


def register_frontend(
    app: FastAPI,
    *,
    worker: DispatcherWorker | None,
    config_store: ConfigStore | DatabaseConfigStore | None,
    persona_store: PersonaStore | None,
    avatar_store: AvatarStore | None,
    home_assistant_manager: HomeAssistantManager | None,
    avatar_upload_root: Path,
    adapters: AdapterRegistry,
    database: Database | None,
    enable_dev_endpoints: bool | None,
) -> None:
    admin_root = Path(__file__).parent.parent / "admin"
    app.mount("/admin/legacy", StaticFiles(directory=admin_root), name="admin-legacy")
    admin_dist = Path(__file__).resolve().parents[3] / "web" / "apps" / "dist"
    admin_spa_ready = (admin_dist / "index.html").is_file()
    if admin_spa_ready:
        app.mount(
            "/admin/assets",
            StaticFiles(directory=admin_dist / "assets"),
            name="admin-assets",
        )
    chat_root = Path(__file__).parent.parent / "chat_ui"
    app.mount(
        "/chat/debug/assets", StaticFiles(directory=chat_root), name="chat-debug-assets"
    )
    pet_ui_root = Path(__file__).parent.parent / "pet_ui"
    app.mount(
        "/desktop/pet",
        StaticFiles(directory=pet_ui_root, html=True),
        name="desktop-pet",
    )
    chat_dist = Path(__file__).resolve().parents[3] / "web" / "apps" / "dist"
    chat_spa_ready = (chat_dist / "index.html").is_file()
    chat_dist_assets = chat_dist / "assets"
    if chat_dist_assets.is_dir():
        app.mount("/chat/assets", StaticFiles(directory=chat_dist_assets), name="chat-assets")
    chat_dist_icons = chat_dist / "icons"
    if chat_dist_icons.is_dir():
        app.mount("/chat/icons", StaticFiles(directory=chat_dist_icons), name="chat-icons")
    avatar_assets_root = Path(__file__).parent.parent / "avatar" / "assets"
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
        async def admin_redirect(rest: str = "") -> RedirectResponse:
            # 管理后台已并入主应用（/chat/admin），旧入口 307 保留深链
            target = "/chat/admin" + (f"/{rest}" if rest else "")
            return RedirectResponse(target, status_code=307)

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

    if chat_spa_ready:

        @app.get("/chat/{rest:path}", include_in_schema=False)
        async def chat_spa(rest: str) -> FileResponse:
            # 合并后的 SPA 回退：/chat/admin 等子路由均由前端路由接管
            return FileResponse(chat_dist / "index.html")

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
        modules = getattr(app.state, "module_registry", None)
        if modules is not None:
            result["modules"] = {
                name: {"state": state, "reason_code": modules.reason_codes.get(name)}
                for name, state in modules.states.items()
            }
            if any(state in {"degraded", "blocked"} for state in modules.states.values()):
                result["status"] = "degraded"
        if dispatcher is not None:
            result["dispatcher"] = dispatcher
        if config_store is not None:
            config_status = {
                "version": config_store.current.version,
                "content_hash": config_store.current.content_hash,
                "last_error": config_store.last_error,
            }
            result["configuration"] = config_status
            if config_store.last_error is not None:
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
        if config_store is not None:
            # 前端据此决定定位授权节奏(每次询问/会话内允许); 不含任何密钥。
            result["location_policy"] = {
                "tools_enabled": config_store.current.config.tools.enabled,
                "precise": (
                    config_store.current.config.tools.query.precise_location_policy
                ),
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
    async def adapters_view() -> dict[str, object]:
        return {
            "adapters": [
                manifest.model_dump(mode="json") for manifest in adapters.list_manifests()
            ]
        }

    @app.get("/api/v1/meta/config", tags=["system"])
    async def configuration() -> dict[str, object]:
        if config_store is None:
            return {"configured": False}
        snapshot = config_store.current
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
    if dev_enabled and database is not None:
        app.include_router(create_event_router(database))
