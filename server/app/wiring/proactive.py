"""PERE-03（docs/09 §8）组合根拆分——主动投递栈与感知循环。

从 main.py 原样搬移：ProactiveDeliveryService 装配、SAFE-02 告警状态机
接线、各调度器投递器闭包、HA 主动引擎、MQTT 在场桥、三个感知循环。
R18 的提醒投递闭包原先经占位变量晚绑定 proactive_delivery，搬移后
直接捕获本模块内的真实实例，语义不变。
"""

from __future__ import annotations

from functools import partial
from typing import Any, cast
from uuid import UUID

from fastapi import FastAPI

from app.browser_awareness import (
    BrowserAwarenessAnalyzer,
    BrowserAwarenessGateway,
    BrowserAwarenessLoop,
    BrowserAwarenessResolver,
    BrowserAwarenessTabHints,
    LlmBrowserAnalyzer,
)
from app.cognition import (
    CognitiveCycle,
    PlanCompletionReporter,
)
from app.config import DatabaseConfigStore
from app.db import Database
from app.devices import DeviceTargetResolver, MqttPresenceBridge
from app.devices.mqtt_client import MqttDeviceClient
from app.home_assistant import HomeAssistantManager, HomeAssistantProactiveEngine
from app.jobs import JobEngine, cancel_turn_delegations
from app.jobs.delegated import DelegatedJobWorker
from app.mail import MailClient
from app.mail_awareness import LlmMailAnalyzer, MailAwarenessLoop
from app.memory import MemoryIngester
from app.memory.store import MemoryStore
from app.output import ProactiveDeliveryService
from app.output.proactive import DesktopCommandGateway
from app.perception import PerceptionPipeline
from app.push import PushSubscriptionStore, WebPushAdapter
from app.safety import ActivityTracker, SafetyActivityScheduler, SafetyAlertService
from app.schemas.common import PrivacyLevel
from app.screen_awareness import (
    ScreenAwarenessAnalyzer,
    ScreenAwarenessGateway,
    ScreenAwarenessLoop,
    ScreenAwarenessResolver,
)
from app.timeline.store import TimelineStore
from app.tools.screen import CapabilityScreenAnalyzer

from ..api import create_admin_safety_router
from ..model_capabilities import CapabilityModelService


def register_proactive_stack(
    app: FastAPI,
    *,
    config: DatabaseConfigStore,
    database: Database,
    chat_service: Any,
    websocket_manager: Any,
    device_target_resolver: DeviceTargetResolver | None,
    device_command_gateway: Any | None,
    voice_manager: Any | None,
    push_subscription_store: PushSubscriptionStore | None,
    deleg_worker: DelegatedJobWorker | None,
    plan_completion_reporter: PlanCompletionReporter | None,
    activity_tracker: ActivityTracker,
    job_engine: JobEngine | None,
    admin_token: str | None,
    cognitive_cycle: CognitiveCycle | None,
    task_scheduler: Any | None,
    goal_reminder_scheduler: Any | None,
    focus_scheduler: Any | None,
    daily_brief_scheduler: Any | None,
    daily_review_scheduler: Any | None,
    timeline_store: TimelineStore | None,
    home_assistant_manager: HomeAssistantManager | None,
    mqtt_client: MqttDeviceClient | None,
    perception_pipeline: PerceptionPipeline | None,
) -> tuple[
    ProactiveDeliveryService,
    SafetyAlertService,
    SafetyActivityScheduler,
    HomeAssistantProactiveEngine | None,
    MqttPresenceBridge | None,
]:
    """装配主动投递栈；返回 lifespan 需要的服务实例。"""
    # 主动投递栈只依赖配置/数据库/通道组件，不依赖 Home Assistant；
    # 误挂在 HA 条件下会在 HA 缺席时让提醒/简报/回顾/安全告警全部静默失效。
    proactive_delivery = ProactiveDeliveryService(
        database,
        config,
        chat_service,
        websocket_manager,
        device_resolver=device_target_resolver,
        device_gateway=(
            cast(DesktopCommandGateway, device_command_gateway)
            if device_command_gateway is not None
            else None
        ),
        voice_broadcaster=voice_manager,
        push_adapter=(
            WebPushAdapter(push_subscription_store, config_store=config)
            if push_subscription_store is not None
            else None
        ),
    )
    app.state.proactive_delivery_service = proactive_delivery
    if deleg_worker is not None:
        # DELEG：任务完成经主动输出通道汇报（Web/桌面通知/推送/语音）
        deleg_worker.set_deliver(proactive_delivery.deliver)
    if plan_completion_reporter is not None:
        # BTL-03：计划执行完成主动汇报结果
        plan_completion_reporter.set_deliver(proactive_delivery.deliver)
    # SAFE-02：告警状态机（critical 升级链 + 聊天确认意图 + Timeline）
    safety_alert_service = SafetyAlertService(
        database,
        config,
        proactive_delivery.deliver,
        timeline=timeline_store,
        mailer=MailClient(config),
    )
    app.state.safety_alert_service = safety_alert_service
    chat_service.set_safety(safety_alert_service)
    chat_service.set_activity_tracker(activity_tracker)
    if job_engine is not None:
        # DELEG：取消回合即撤回该回合委派的后台任务
        chat_service.set_deleg_canceller(
            partial(cancel_turn_delegations, job_engine, database)
        )
    app.include_router(
        create_admin_safety_router(
            safety_alert_service, admin_token=admin_token
        )
    )
    # SAFE-01：久未活动调度器（外部信号可经 activity_tracker.record 注入）
    activity_scheduler = SafetyActivityScheduler(
        database,
        config,
        proactive_delivery.deliver,
        activity_tracker,
        cognitive_cycle=cognitive_cycle,
    )
    app.state.safety_activity_scheduler = activity_scheduler
    app.state.safety_activity_tracker = activity_tracker

    async def deliver_task_reminder(
        text: str,
        *,
        user_id: UUID,
        task_id: UUID,
        privacy_level: PrivacyLevel,
        trigger_kind: str,
    ) -> list[str] | None:
        result = await proactive_delivery.deliver(
            text,
            entity_id="task",
            rule_id=f"task:{task_id}",
            trigger_kind=trigger_kind,
            privacy_level=privacy_level,
            target_user_id=user_id,
        )
        if result is None:
            return None
        return list(result.delivered_channels)

    async def deliver_goal_reminder(
        text: str,
        *,
        user_id: UUID,
        goal_id: UUID,
        privacy_level: str,
        trigger_kind: str,
    ) -> list[str] | None:
        result = await proactive_delivery.deliver(
            text,
            entity_id="goal",
            rule_id=f"goal:{goal_id}",
            trigger_kind=trigger_kind,
            privacy_level=PrivacyLevel(privacy_level),
            target_user_id=user_id,
        )
        if result is None:
            return None
        return list(result.delivered_channels)

    if task_scheduler is not None:
        task_scheduler.set_deliverer(deliver_task_reminder)
    if goal_reminder_scheduler is not None:
        goal_reminder_scheduler.set_deliverer(deliver_goal_reminder)
    if focus_scheduler is not None:

        async def deliver_focus_nudge(
            text: str,
            *,
            user_id: UUID,
            entity_id: str,
            trigger_kind: str,
            privacy_level: str,
        ) -> list[str] | None:
            result = await proactive_delivery.deliver(
                text,
                entity_id=entity_id,
                rule_id=entity_id,
                trigger_kind=trigger_kind,
                privacy_level=PrivacyLevel(privacy_level),
                target_user_id=user_id,
            )
            if result is None:
                return None
            return list(result.delivered_channels)

        focus_scheduler.set_deliverer(deliver_focus_nudge)
    if daily_brief_scheduler is not None:

        async def deliver_daily_brief(
            text: str,
            *,
            user_id: UUID,
            brief_id: UUID,
            privacy_level: PrivacyLevel,
            trigger_kind: str,
        ) -> list[str] | None:
            result = await proactive_delivery.deliver(
                text,
                entity_id="brief",
                rule_id=f"brief:{brief_id}",
                trigger_kind=trigger_kind,
                privacy_level=privacy_level,
                target_user_id=user_id,
            )
            if result is None:
                return None
            return list(result.delivered_channels)

        daily_brief_scheduler.set_deliverer(deliver_daily_brief)
    if daily_review_scheduler is not None:

        async def deliver_daily_review(
            text: str,
            *,
            user_id: UUID,
            review_id: UUID,
            privacy_level: PrivacyLevel,
            trigger_kind: str,
        ) -> list[str] | None:
            result = await proactive_delivery.deliver(
                text,
                entity_id="review",
                rule_id=f"review:{review_id}",
                trigger_kind=trigger_kind,
                privacy_level=privacy_level,
                target_user_id=user_id,
            )
            if result is None:
                return None
            return list(result.delivered_channels)

        daily_review_scheduler.set_deliverer(deliver_daily_review)

    home_assistant_proactive: HomeAssistantProactiveEngine | None = None
    if home_assistant_manager is not None:
        home_assistant_proactive = HomeAssistantProactiveEngine(
            database,
            config,
            home_assistant_manager.get_state,
            proactive_delivery.deliver,
            cognitive_cycle=cognitive_cycle,
            perception_pipeline=perception_pipeline,
            safety=safety_alert_service,
        )
        home_assistant_manager.set_state_change_handler(
            home_assistant_proactive.on_state_change
        )
        app.state.home_assistant_proactive_engine = home_assistant_proactive

    mqtt_presence_bridge: MqttPresenceBridge | None = None
    if mqtt_client is not None and perception_pipeline is not None:
        mqtt_presence_bridge = MqttPresenceBridge(
            database,
            perception_pipeline,
            proactive_deliver=proactive_delivery.deliver,
        )
        mqtt_client.set_signal_handler(mqtt_presence_bridge.handle)
        app.state.mqtt_presence_bridge = mqtt_presence_bridge

    return (
        proactive_delivery,
        safety_alert_service,
        activity_scheduler,
        home_assistant_proactive,
        mqtt_presence_bridge,
    )


def register_awareness_loops(
    app: FastAPI,
    *,
    config: DatabaseConfigStore,
    database: Database,
    device_target_resolver: DeviceTargetResolver,
    device_command_gateway: Any,
    capability_models: CapabilityModelService,
    timeline_store: TimelineStore,
    memory_store: MemoryStore | None,
    perception_pipeline: PerceptionPipeline | None,
    proactive_delivery: ProactiveDeliveryService,
    cognitive_cycle: CognitiveCycle | None,
) -> tuple[ScreenAwarenessLoop, BrowserAwarenessLoop, MailAwarenessLoop]:
    """装配屏幕/浏览/邮件三个感知循环；返回 lifespan 启停所需实例。"""
    screen_awareness_loop = ScreenAwarenessLoop(
        config_store=config,
        database=database,
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
        proactive_deliver=proactive_delivery.deliver,
        cognitive_cycle=cognitive_cycle,
    )
    app.state.screen_awareness_loop = screen_awareness_loop
    browser_awareness_loop = BrowserAwarenessLoop(
        config_store=config,
        database=database,
        resolver=cast(BrowserAwarenessResolver, device_target_resolver),
        gateway=cast(BrowserAwarenessGateway, device_command_gateway),
        analyzer=cast(BrowserAwarenessAnalyzer, LlmBrowserAnalyzer(config)),
        timeline=timeline_store,
        memory_ingester=(
            MemoryIngester(memory_store) if memory_store is not None else None
        ),
        perception_pipeline=perception_pipeline,
        proactive_deliver=proactive_delivery.deliver,
        cognitive_cycle=cognitive_cycle,
        tab_hints=cast(BrowserAwarenessTabHints, device_command_gateway),
    )
    app.state.browser_awareness_loop = browser_awareness_loop
    mail_awareness_loop = MailAwarenessLoop(
        config_store=config,
        database=database,
        reader=MailClient(config),
        analyzer=LlmMailAnalyzer(config),
        timeline=timeline_store,
        memory_ingester=(
            MemoryIngester(memory_store) if memory_store is not None else None
        ),
        perception_pipeline=perception_pipeline,
        proactive_deliver=proactive_delivery.deliver,
        cognitive_cycle=cognitive_cycle,
    )
    app.state.mail_awareness_loop = mail_awareness_loop
    return screen_awareness_loop, browser_awareness_loop, mail_awareness_loop
