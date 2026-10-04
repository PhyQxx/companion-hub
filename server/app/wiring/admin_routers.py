"""PERE-03（docs/09 §8）组合根拆分——数据库配置态的 Admin 前置路由。

从 main.py 原样搬移：Admin token 解析、配置/语音/MCP/技能/人格/
时间线/记忆/删除台账/备份等不依赖运行时数据库装配的路由。
搬移时删除了从未被引用的 trigger_calendar_sync 死代码。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI

from app import __version__
from app.cognition import ActionRegistry
from app.config import DatabaseConfigStore
from app.db import Database
from app.home_assistant import HomeAssistantManager
from app.integrations.mcp import McpManager
from app.integrations.mcp.actions import sync_mcp_actions
from app.memory.store import MemoryStore
from app.persona import PersonaStore
from app.skills.connections import SkillConnectionStore, SkillHttpClient
from app.skills.credentials import SkillCredentialStore
from app.skills.generator import SkillDraftGenerator
from app.skills.runtime import SkillToolProvider
from app.skills.store import SkillStore
from app.timeline.store import TimelineStore
from app.xiaoai_config import XiaoAiConfigMaterializer

from ..api import (
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
    create_admin_screen_awareness_router,
    create_admin_senseaudio_router,
    create_admin_skills_router,
    create_admin_tasks_router,
    create_admin_theme_router,
    create_admin_timeline_router,
    create_deletion_ledger_router,
    create_logs_stream_router,
)
from ..api.admin_config import set_runtime_admin_token
from .domain import DomainAssembly


def register_admin_routers(
    app: FastAPI,
    *,
    config: DatabaseConfigStore,
    database: Database | None,
    admin_token: str | None,
    home_assistant_manager: HomeAssistantManager | None,
    xiaoai_materializer: XiaoAiConfigMaterializer | None,
    ha_proactive_test: Callable[[], Awaitable[bool]],
    mcp_manager: McpManager | None,
    skill_store: SkillStore | None,
    skill_generator: SkillDraftGenerator | None,
    skill_tool_provider: SkillToolProvider | None,
    skill_connections: SkillConnectionStore | None,
    skill_credentials: SkillCredentialStore | None,
    skill_http_client: SkillHttpClient | None,
    action_registry: ActionRegistry,
    persona_store: PersonaStore | None,
    timeline_store: TimelineStore | None,
    memory_store: MemoryStore | None,
) -> str | None:
    """装配 Admin 前置路由；返回解析后的 runtime_admin_token 供后续段复用。"""
    runtime_admin_token = admin_token if admin_token is not None else os.getenv("ARIA_ADMIN_TOKEN")
    set_runtime_admin_token(runtime_admin_token)

    async def reconfigure_integrations() -> None:
        if home_assistant_manager is not None:
            await home_assistant_manager.reconfigure()
        if xiaoai_materializer is not None:
            await xiaoai_materializer.write()

    app.include_router(
        create_admin_config_router(
            config,
            admin_token=runtime_admin_token,
            database=database,
            on_publish=reconfigure_integrations,
            on_proactive_test=ha_proactive_test,
        )
    )
    app.include_router(
        create_admin_senseaudio_router(
            config,
            admin_token=runtime_admin_token,
            database=database,
        )
    )
    app.include_router(
        create_admin_mcp_router(
            mcp_manager,
            admin_token=runtime_admin_token,
        )
    )
    if skill_store is not None:
        app.include_router(
            create_admin_skills_router(
                skill_store,
                admin_token=runtime_admin_token,
                generator=skill_generator,
                tool_provider=skill_tool_provider,
                connections=skill_connections,
                credentials=skill_credentials,
                http_client=skill_http_client,
                action_registry=action_registry,
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
    return runtime_admin_token


def register_admin_data_routers(
    app: FastAPI,
    *,
    database: Database,
    admin_token: str | None,
    domain: DomainAssembly,
) -> None:
    """装配依赖运行时数据库的 Admin 数据路由；调用方保证 database 非空。"""
    # FR-S3 数据导出/导入：伴侣数据 JSON 档案，凭据与机器状态不导出
    app.include_router(
        create_admin_export_router(
            database=database,
            admin_token=admin_token,
            hub_version=__version__,
        )
    )
    app.include_router(
        create_admin_dashboard_router(
            database,
            admin_token=admin_token,
            version=__version__,
        )
    )
    app.include_router(
        create_logs_stream_router(
            admin_token=admin_token,
        )
    )
    if domain.job_engine is not None:
        app.include_router(
            create_admin_jobs_router(
                domain.job_engine,
                admin_token=admin_token,
            )
        )
    if domain.task_store is not None:
        app.include_router(
            create_admin_tasks_router(
                domain.task_store,
                admin_token=admin_token,
            )
        )
    if (
        domain.workflow_service is not None
        and domain.home_scene_service is not None
        and domain.meeting_service is not None
        and domain.daily_brief_service is not None
        and domain.daily_review_service is not None
    ):
        app.include_router(
            create_admin_butler_router(
                database=database,
                workflows=domain.workflow_service,
                scenes=domain.home_scene_service,
                meetings=domain.meeting_service,
                briefs=domain.daily_brief_service,
                reviews=domain.daily_review_service,
                admin_token=admin_token,
                drafts=domain.workflow_draft_store,
                distiller=domain.plan_distiller,
            )
        )
    app.include_router(
        create_admin_screen_awareness_router(
            domain.timeline_store,
            admin_token=admin_token,
        )
    )
    app.include_router(
        create_admin_browser_awareness_router(
            domain.timeline_store,
            admin_token=admin_token,
        )
    )
    if domain.avatar_store is not None:
        app.include_router(
            create_admin_avatar_router(
                domain.avatar_store,
                admin_token=admin_token,
                asset_importer=domain.avatar_importer,
            )
        )
    if domain.theme_store is not None:
        app.include_router(
            create_admin_theme_router(
                domain.theme_store,
                admin_token=admin_token,
            )
        )
