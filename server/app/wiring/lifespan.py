"""PERE-03（docs/09 §8）组合根拆分——应用 lifespan 启停序列。

从 main.py 原样搬移：启动序列（配置装载/记忆嵌入探测/外部管理器/
调度器/感知循环）与逆序停机序列。原实现经闭包晚绑定读取 create_app
局部变量；这里改为 LifespanDeps 属性读取——create_app 在 return 前
统一灌入最终值，启动发生在装配完成后，语义等价。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import cast

from fastapi import FastAPI

from app.appearance import ThemeStore
from app.avatar import AvatarStore
from app.browser_awareness import BrowserAwarenessLoop
from app.bus import DispatcherWorker
from app.calendar import CalDavSyncScheduler, GoogleCalendarSyncScheduler
from app.chat import ChatService
from app.cognition import ActionRegistry
from app.config import ConfigStore, ConfigWatcher, DatabaseConfigStore
from app.db import Database
from app.devices import MqttPresenceBridge
from app.devices.mqtt_client import MqttDeviceClient
from app.home_assistant import HomeAssistantManager, HomeAssistantProactiveEngine
from app.integrations.mcp import McpManager
from app.jobs import DelegatedJobWorker
from app.llm.provider import EnvSecretProvider
from app.mail_awareness import MailAwarenessLoop
from app.memory import build_embedding_provider, probe_embedding_provider
from app.memory.store import MemoryStore
from app.observability import apply_observability
from app.observability.selfcheck import DailySelfCheckScheduler
from app.perception import PerceptionPipeline
from app.persona import PersonaStore
from app.pnkx import PnkxLifeClient
from app.runtime import TurnCoordinator
from app.screen_awareness import ScreenAwarenessLoop
from app.skills.actions import sync_skill_actions
from app.skills.audit import SkillAuditScheduler
from app.skills.connections import SkillHttpClient
from app.skills.store import SkillStore
from app.tasks import TaskScheduler
from app.tasks.brief_scheduler import DailyBriefScheduler
from app.tasks.goal_scheduler import GoalReminderScheduler
from app.tasks.review_scheduler import DailyReviewScheduler
from app.todo.sync_scheduler import TodoSyncScheduler
from app.workflows.drafts import PlanDistiller
from app.xiaoai_config import XiaoAiConfigMaterializer

from .registry import ModuleRegistry, ModuleSpec


@dataclass
class LifespanDeps:
    """lifespan 读取的全部依赖快照；create_app 返回前灌入最终值。"""

    runtime_config: ConfigStore | DatabaseConfigStore | None = None
    runtime_database: Database | None = None
    owns_database: bool = False
    xiaoai_materializer: XiaoAiConfigMaterializer | None = None
    persona_store: PersonaStore | None = None
    memory_store: MemoryStore | None = None
    home_assistant_manager: HomeAssistantManager | None = None
    mqtt_client: MqttDeviceClient | None = None
    avatar_store: AvatarStore | None = None
    theme_store: ThemeStore | None = None
    runtime_chat_service: ChatService | None = None
    turn_coordinator: TurnCoordinator | None = None
    config_watcher: ConfigWatcher | None = None
    worker: DispatcherWorker | None = None
    deleg_worker: DelegatedJobWorker | None = None
    screen_awareness_loop: ScreenAwarenessLoop | None = None
    browser_awareness_loop: BrowserAwarenessLoop | None = None
    mail_awareness_loop: MailAwarenessLoop | None = None
    mcp_manager: McpManager | None = None
    safety_alert_service: object | None = None
    activity_scheduler: object | None = None
    task_scheduler: TaskScheduler | None = None
    goal_reminder_scheduler: GoalReminderScheduler | None = None
    focus_scheduler: object | None = None
    daily_brief_scheduler: DailyBriefScheduler | None = None
    daily_review_scheduler: DailyReviewScheduler | None = None
    todo_sync_scheduler: TodoSyncScheduler | None = None
    caldav_sync_scheduler: CalDavSyncScheduler | None = None
    google_calendar_sync_scheduler: GoogleCalendarSyncScheduler | None = None
    skill_store: SkillStore | None = None
    action_registry: ActionRegistry | None = None
    skill_http_client: SkillHttpClient | None = None
    skill_audit_scheduler: SkillAuditScheduler | None = None
    pnkx_life_client: PnkxLifeClient | None = None
    plan_distiller: PlanDistiller | None = None
    home_assistant_proactive: HomeAssistantProactiveEngine | None = None
    mqtt_presence_bridge: MqttPresenceBridge | None = None
    perception_pipeline: PerceptionPipeline | None = None
    self_check_scheduler: DailySelfCheckScheduler | None = None


def _core_modules(deps: LifespanDeps) -> ModuleRegistry:
    async def load_memory() -> None:
        if deps.memory_store is not None:
            from app.memory.replay import import_deletion_journal, replay_deletions

            if deps.memory_store.deletion_journal is not None:
                await import_deletion_journal(
                    deps.memory_store.database, deps.memory_store.deletion_journal
                )
            # Replay before retrieval or maintenance can see restored sources.
            await replay_deletions(deps.memory_store.database, dry_run=False)
        if deps.persona_store is not None:
            await deps.persona_store.load()
        if deps.memory_store is not None and deps.runtime_config is not None:
            # SEMB（docs/09 §2）：配置启用语义嵌入时先做连通性探测，
            # 失败回落内置哈希嵌入（词法级检索），不阻断启动。
            embedding_provider = build_embedding_provider(
                deps.runtime_config.current.config.embeddings, EnvSecretProvider()
            )
            if embedding_provider is not None:
                if await probe_embedding_provider(embedding_provider):
                    deps.memory_store.set_embedding_provider(embedding_provider)
                    logging.getLogger(__name__).info(
                        "semantic embedding enabled model=%s dimension=%d",
                        embedding_provider.model_name,
                        embedding_provider.dimension,
                    )
                else:
                    logging.getLogger(__name__).warning(
                        "semantic embedding unavailable, falling back to hashing embedder"
                    )

    async def recover_conversation() -> None:
        if deps.runtime_chat_service is not None:
            await deps.runtime_chat_service.recover_incomplete_turns()
            start_postcommit = getattr(deps.runtime_chat_service, "start_postcommit_worker", None)
            if start_postcommit is not None:
                start_postcommit()
        if deps.turn_coordinator is not None:
            # 重启后把不安全的未完成回合标记为 cancelled，并清理遗留音频/麦克风租约
            recovered_turns = await deps.turn_coordinator.recover_after_restart()
            await deps.turn_coordinator.expire_stale_leases()
            if recovered_turns:
                logging.getLogger(__name__).info(
                    "recovered %s unsafe turns after restart", recovered_turns
                )

    async def close_skills() -> None:
        if deps.skill_http_client is not None:
            await deps.skill_http_client.close()

    async def drain_conversation() -> None:
        if deps.runtime_chat_service is not None:
            await deps.runtime_chat_service.drain_background_work()

    specs = [
        ModuleSpec("skills", provides=("skill_client",), stop=close_skills),
        ModuleSpec("memory", provides=("memory_context",), start=load_memory),
        ModuleSpec(
            "conversation",
            requires=("memory", "skills"),
            provides=("conversation",),
            start=recover_conversation,
            stop=drain_conversation,
        ),
    ]

    def add_service(
        name: str,
        service: object | None,
        *,
        start_method: str | None = "start",
        stop_method: str = "stop",
        requires: tuple[str, ...] = ("conversation",),
        critical: bool = False,
    ) -> None:
        if service is None:
            return

        async def invoke(method: str) -> None:
            callback = cast(Callable[[], object], getattr(service, method))
            value = callback()
            if inspect.isawaitable(value):
                await value

        async def start() -> None:
            if start_method is not None:
                await invoke(start_method)

        async def stop() -> None:
            await invoke(stop_method)

        specs.append(ModuleSpec(name, requires=requires, critical=critical, start=start, stop=stop))

    add_service(
        "pnkx-client", deps.pnkx_life_client, start_method=None, stop_method="close", requires=()
    )
    add_service("home-assistant", deps.home_assistant_manager, requires=("memory",))
    add_service("mqtt", deps.mqtt_client, requires=("memory",))
    add_service("config-watcher", deps.config_watcher, critical=True)
    add_service("dispatcher", deps.worker, critical=True)
    add_service("delegated-jobs", deps.deleg_worker)
    add_service("screen-awareness", deps.screen_awareness_loop)
    add_service("browser-awareness", deps.browser_awareness_loop)
    add_service("mail-awareness", deps.mail_awareness_loop)
    add_service("mcp", deps.mcp_manager)
    add_service("safety-alerts", deps.safety_alert_service, start_method="resume", critical=True)
    add_service("activity", deps.activity_scheduler)
    add_service("task-reminders", deps.task_scheduler)
    add_service("goal-reminders", deps.goal_reminder_scheduler)
    add_service("focus", deps.focus_scheduler)
    add_service("daily-brief", deps.daily_brief_scheduler)
    add_service("daily-review", deps.daily_review_scheduler)
    add_service("todo-sync", deps.todo_sync_scheduler)
    add_service("caldav-sync", deps.caldav_sync_scheduler)
    add_service("google-calendar-sync", deps.google_calendar_sync_scheduler)
    add_service("workflow-distillation", deps.plan_distiller)
    add_service("skill-audit", deps.skill_audit_scheduler)
    add_service("self-check", deps.self_check_scheduler)
    add_service("home-proactive", deps.home_assistant_proactive, start_method=None)
    add_service("mqtt-presence", deps.mqtt_presence_bridge, start_method=None)
    add_service("perception", deps.perception_pipeline, start_method=None)
    return ModuleRegistry(specs)


def build_lifespan(
    deps: LifespanDeps,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """构造 lifespan 协程函数；依赖经 deps 属性在启动时读取。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        modules = _core_modules(deps)
        app.state.module_registry = modules
        try:
            # Constructed clients need cleanup even when config loading fails.
            await modules.start("skills")
            if deps.pnkx_life_client is not None:
                await modules.start("pnkx-client")
            if deps.runtime_config is not None:
                await deps.runtime_config.load()
                apply_observability(deps.runtime_config.current.config)
                if deps.xiaoai_materializer is not None:
                    await deps.xiaoai_materializer.write()
            await modules.start("memory")
            if deps.home_assistant_manager is not None:
                await modules.start("home-assistant")
            if deps.mqtt_client is not None:
                await modules.start("mqtt")
            if deps.avatar_store is not None:
                await deps.avatar_store.load_builtin_packs()
            if deps.theme_store is not None:
                await deps.theme_store.load_builtin_themes()
            await modules.start("conversation")
            await modules.start_all()
            if deps.skill_store is not None and deps.action_registry is not None:
                # 技能 S3：启动时把已启用技能的写操作同步进动作目录
                try:
                    sync_skill_actions(deps.action_registry, await deps.skill_store.list())
                except Exception:
                    logging.getLogger(__name__).warning(
                        "skill write action sync failed on startup", exc_info=True
                    )
            yield
        finally:
            try:
                await modules.stop_all()
            finally:
                if deps.owns_database and deps.runtime_database is not None:
                    await deps.runtime_database.close()

    return lifespan
