"""PERE-03（docs/09 §8）组合根拆分——应用 lifespan 启停序列。

从 main.py 原样搬移：启动序列（配置装载/记忆嵌入探测/外部管理器/
调度器/感知循环）与逆序停机序列。原实现经闭包晚绑定读取 create_app
局部变量；这里改为 LifespanDeps 属性读取——create_app 在 return 前
统一灌入最终值，启动发生在装配完成后，语义等价。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

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
from app.xiaoai_config import XiaoAiConfigMaterializer


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
    home_assistant_proactive: HomeAssistantProactiveEngine | None = None
    mqtt_presence_bridge: MqttPresenceBridge | None = None
    perception_pipeline: PerceptionPipeline | None = None
    self_check_scheduler: DailySelfCheckScheduler | None = None


def build_lifespan(
    deps: LifespanDeps,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """构造 lifespan 协程函数；依赖经 deps 属性在启动时读取。"""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if deps.runtime_config is not None:
            await deps.runtime_config.load()
            apply_observability(deps.runtime_config.current.config)
            if deps.xiaoai_materializer is not None:
                await deps.xiaoai_materializer.write()
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
        if deps.home_assistant_manager is not None:
            await deps.home_assistant_manager.start()
        if deps.mqtt_client is not None:
            await deps.mqtt_client.start()
        if deps.avatar_store is not None:
            await deps.avatar_store.load_builtin_packs()
        if deps.theme_store is not None:
            await deps.theme_store.load_builtin_themes()
        if deps.runtime_chat_service is not None:
            await deps.runtime_chat_service.recover_incomplete_turns()
        if deps.turn_coordinator is not None:
            # 重启后把不安全的未完成回合标记为 cancelled，并清理遗留音频/麦克风租约
            recovered_turns = await deps.turn_coordinator.recover_after_restart()
            await deps.turn_coordinator.expire_stale_leases()
            if recovered_turns:
                logging.getLogger(__name__).info(
                    "recovered %s unsafe turns after restart", recovered_turns
                )
        if deps.config_watcher is not None:
            await deps.config_watcher.start()
        if deps.worker is not None:
            await deps.worker.start()
        if deps.deleg_worker is not None:
            deps.deleg_worker.start()
        if deps.screen_awareness_loop is not None:
            deps.screen_awareness_loop.start()
        if deps.browser_awareness_loop is not None:
            deps.browser_awareness_loop.start()
        if deps.mail_awareness_loop is not None:
            deps.mail_awareness_loop.start()
        if deps.mcp_manager is not None:
            deps.mcp_manager.start()
        if deps.safety_alert_service is not None:
            await deps.safety_alert_service.resume()  # type: ignore[attr-defined]
        if deps.activity_scheduler is not None:
            await deps.activity_scheduler.start()  # type: ignore[attr-defined]
        if deps.task_scheduler is not None:
            deps.task_scheduler.start()
        if deps.goal_reminder_scheduler is not None:
            deps.goal_reminder_scheduler.start()
        if deps.focus_scheduler is not None:
            deps.focus_scheduler.start()  # type: ignore[attr-defined]
        if deps.daily_brief_scheduler is not None:
            deps.daily_brief_scheduler.start()
        if deps.daily_review_scheduler is not None:
            deps.daily_review_scheduler.start()
        if deps.todo_sync_scheduler is not None:
            deps.todo_sync_scheduler.start()
        if deps.caldav_sync_scheduler is not None:
            deps.caldav_sync_scheduler.start()
        if deps.google_calendar_sync_scheduler is not None:
            deps.google_calendar_sync_scheduler.start()
        if deps.skill_store is not None and deps.action_registry is not None:
            # 技能 S3：启动时把已启用技能的写操作同步进动作目录
            try:
                sync_skill_actions(deps.action_registry, await deps.skill_store.list())
            except Exception:
                logging.getLogger(__name__).warning(
                    "skill write action sync failed on startup", exc_info=True
                )
        if deps.skill_audit_scheduler is not None:
            deps.skill_audit_scheduler.start()
        if deps.self_check_scheduler is not None:
            deps.self_check_scheduler.start()
        try:
            yield
        finally:
            if deps.self_check_scheduler is not None:
                await deps.self_check_scheduler.stop()
            if deps.skill_audit_scheduler is not None:
                await deps.skill_audit_scheduler.stop()
            if deps.skill_http_client is not None:
                await deps.skill_http_client.close()
            if deps.pnkx_life_client is not None:
                await deps.pnkx_life_client.close()
            if deps.todo_sync_scheduler is not None:
                await deps.todo_sync_scheduler.stop()
            if deps.caldav_sync_scheduler is not None:
                await deps.caldav_sync_scheduler.stop()
            if deps.google_calendar_sync_scheduler is not None:
                await deps.google_calendar_sync_scheduler.stop()
            if deps.daily_review_scheduler is not None:
                await deps.daily_review_scheduler.stop()
            if deps.daily_brief_scheduler is not None:
                await deps.daily_brief_scheduler.stop()
            if deps.goal_reminder_scheduler is not None:
                await deps.goal_reminder_scheduler.stop()
            if deps.task_scheduler is not None:
                await deps.task_scheduler.stop()
            if deps.screen_awareness_loop is not None:
                await deps.screen_awareness_loop.stop()
            if deps.browser_awareness_loop is not None:
                await deps.browser_awareness_loop.stop()
            if deps.mail_awareness_loop is not None:
                await deps.mail_awareness_loop.stop()
            if deps.mcp_manager is not None:
                await deps.mcp_manager.stop()
            if deps.safety_alert_service is not None:
                await deps.safety_alert_service.stop()  # type: ignore[attr-defined]
            if deps.activity_scheduler is not None:
                await deps.activity_scheduler.stop()  # type: ignore[attr-defined]
            if deps.home_assistant_proactive is not None:
                await deps.home_assistant_proactive.stop()
            if deps.mqtt_client is not None:
                await deps.mqtt_client.stop()
            if deps.mqtt_presence_bridge is not None:
                await deps.mqtt_presence_bridge.stop()
            if deps.perception_pipeline is not None:
                await deps.perception_pipeline.stop()
            if deps.runtime_chat_service is not None:
                # 等待仍在执行的后台记忆沉淀收尾，避免丢最后一轮的事实
                await deps.runtime_chat_service.drain_background_work()
            if deps.worker is not None:
                await deps.worker.stop()
            if deps.deleg_worker is not None:
                await deps.deleg_worker.stop()
            if deps.home_assistant_manager is not None:
                await deps.home_assistant_manager.stop()
            if deps.config_watcher is not None:
                await deps.config_watcher.stop()
            if deps.owns_database and deps.runtime_database is not None:
                await deps.runtime_database.close()

    return lifespan
