# ruff: noqa: RUF001
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, time, timedelta
from typing import TypeAlias
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select

from app.chat import MessageView
from app.cognition import CognitiveCycle, CognitiveDecision, DecisionKind, SemanticEvent
from app.config import (
    ConfigStore,
    DatabaseConfigStore,
    HomeAssistantEntityConfig,
    HomeAssistantProactiveRuleConfig,
)
from app.db import AppUserRecord, Database, HomeAssistantProactiveLogRecord
from app.ids import uuid7
from app.schemas import PrivacyLevel

from .models import HomeAssistantError, HomeAssistantState

logger = logging.getLogger(__name__)

DeliveryResult: TypeAlias = tuple[UUID, MessageView] | None
Deliver = Callable[..., Awaitable[DeliveryResult]]
ReadState = Callable[[str], HomeAssistantState]


class HomeAssistantProactiveEngine:
    """Evaluate explicit HA rules and deliver rate-limited assistant messages."""

    def __init__(
        self,
        database: Database,
        config_store: ConfigStore | DatabaseConfigStore,
        read_state: ReadState,
        deliver: Deliver,
        cognitive_cycle: CognitiveCycle | None = None,
    ) -> None:
        self._database = database
        self._config_store = config_store
        self._read_state = read_state
        self._deliver = deliver
        self._cognitive_cycle = cognitive_cycle
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def stop(self) -> None:
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def send_test_message(self) -> DeliveryResult:
        return await self._deliver(
            "主动感知测试成功：我已经能根据 Home Assistant 状态主动给你发送提醒了。",
            entity_id="home_assistant.integration",
            rule_id="manual_test",
            trigger_kind="manual_test",
            privacy_level=PrivacyLevel.L1,
        )

    async def on_state_change(
        self,
        policy: HomeAssistantEntityConfig,
        old_state: HomeAssistantState | None,
        new_state: HomeAssistantState | None,
    ) -> None:
        del old_state
        config = self._config_store.current.config.integrations.home_assistant
        configured = next(
            (item for item in config.entities if item.entity_id == policy.entity_id), None
        )
        if not config.proactive_enabled or configured is None:
            self._cancel_entity(policy.entity_id)
            return
        for rule in configured.proactive_rules:
            key = (configured.entity_id, rule.rule_id)
            if not rule.enabled or not self._matches(rule, new_state):
                self._cancel(key)
                continue
            if key in self._tasks:
                continue
            task = asyncio.create_task(
                self._wait_and_fire(configured.entity_id, rule.rule_id, rule.duration_seconds),
                name=f"ha-proactive-{configured.entity_id}-{rule.rule_id}",
            )
            self._tasks[key] = task

    async def _wait_and_fire(self, entity_id: str, rule_id: str, duration_seconds: int) -> None:
        key = (entity_id, rule_id)
        try:
            if duration_seconds:
                await asyncio.sleep(duration_seconds)
            config = self._config_store.current.config.integrations.home_assistant
            policy = next((x for x in config.entities if x.entity_id == entity_id), None)
            rule = (
                next((x for x in policy.proactive_rules if x.rule_id == rule_id), None)
                if policy is not None
                else None
            )
            if not config.proactive_enabled or policy is None or rule is None or not rule.enabled:
                return
            try:
                state = self._read_state(entity_id)
            except HomeAssistantError:
                state = None
            if not self._matches(rule, state):
                return
            await self._fire(policy, rule, state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("HA proactive rule failed: %s/%s", entity_id, rule_id)
        finally:
            current = self._tasks.get(key)
            if current is asyncio.current_task():
                self._tasks.pop(key, None)

    async def _fire(
        self,
        policy: HomeAssistantEntityConfig,
        rule: HomeAssistantProactiveRuleConfig,
        state: HomeAssistantState | None,
    ) -> None:
        now = datetime.now(UTC)
        reason = await self._gate_reason(policy, rule, now)
        if reason is not None:
            await self._log(policy, rule, now, passed=False, reason=reason)
            return
        message = rule.message or self._render_message(policy, rule, state)
        cognitive_decision: CognitiveDecision | None = None
        if self._cognitive_cycle is not None:
            user_id = await self._active_user_id()
            if user_id is None:
                await self._log(policy, rule, now, passed=False, reason="no_active_user")
                return
            cognitive_decision = await self._cognitive_cycle.evaluate(
                SemanticEvent(
                    event_id=uuid7(),
                    user_id=user_id,
                    kind=rule.kind,
                    summary=f"{policy.display_name} 触发 {rule.kind}",
                    occurred_at=now,
                    privacy_level=PrivacyLevel(policy.privacy_level),
                    confidence=1,
                    evidence_ids=[
                        f"ha:{policy.entity_id}:"
                        f"{_state_changed_at(state, fallback=now).isoformat()}"
                    ],
                    attributes={"message": message},
                    expires_at=now + timedelta(minutes=5),
                )
            )
            if cognitive_decision.decision in {DecisionKind.IGNORE, DecisionKind.RECORD}:
                await self._log(
                    policy,
                    rule,
                    now,
                    passed=False,
                    reason=f"cognitive_{cognitive_decision.decision}",
                )
                return
            message = cognitive_decision.message or message
        if cognitive_decision is None:
            result = await self._deliver(
                message,
                entity_id=policy.entity_id,
                rule_id=rule.rule_id,
                trigger_kind=rule.kind,
                privacy_level=PrivacyLevel(policy.privacy_level),
            )
        else:
            result = await self._deliver(
                message,
                entity_id=policy.entity_id,
                rule_id=rule.rule_id,
                trigger_kind=rule.kind,
                privacy_level=PrivacyLevel(policy.privacy_level),
                cognitive_decision=cognitive_decision,
            )
        if result is None:
            await self._log(policy, rule, now, passed=False, reason="no_active_conversation")
            return
        user_id, created = result
        await self._log(
            policy,
            rule,
            now,
            passed=True,
            user_id=user_id,
            conversation_id=created.conversation_id,
            message=message,
        )

    async def _gate_reason(
        self,
        policy: HomeAssistantEntityConfig,
        rule: HomeAssistantProactiveRuleConfig,
        now: datetime,
    ) -> str | None:
        if policy.privacy_level not in {"L0", "L1"}:
            return "privacy_blocked"
        config = self._config_store.current.config.integrations.home_assistant
        timezone = await self._user_timezone()
        local_now = now.astimezone(timezone)
        critical = rule.kind == "water_leak"
        if self._in_quiet_hours(
            local_now.timetz().replace(tzinfo=None),
            config.proactive_quiet_hours_start,
            config.proactive_quiet_hours_end,
        ) and not (critical and config.proactive_critical_bypasses_quiet_hours):
            return "quiet_hours"
        cooldown_since = now - timedelta(minutes=rule.cooldown_minutes)
        day_start_local = datetime.combine(local_now.date(), time.min, timezone)
        day_start = day_start_local.astimezone(UTC)
        async with self._database.sessions() as session:
            recent = await session.scalar(
                select(HomeAssistantProactiveLogRecord.id)
                .where(
                    HomeAssistantProactiveLogRecord.entity_id == policy.entity_id,
                    HomeAssistantProactiveLogRecord.rule_id == rule.rule_id,
                    HomeAssistantProactiveLogRecord.passed_gate.is_(True),
                    HomeAssistantProactiveLogRecord.created_at >= cooldown_since,
                )
                .limit(1)
            )
            if recent is not None:
                return "cooldown"
            sent_today = await session.scalar(
                select(func.count(HomeAssistantProactiveLogRecord.id)).where(
                    HomeAssistantProactiveLogRecord.passed_gate.is_(True),
                    HomeAssistantProactiveLogRecord.created_at >= day_start,
                )
            )
        if int(sent_today or 0) >= config.proactive_daily_limit:
            return "daily_limit"
        return None

    async def _active_user_id(self) -> UUID | None:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(AppUserRecord.id)
                .where(AppUserRecord.status == "active")
                .order_by(AppUserRecord.created_at)
                .limit(1)
            )
        return value if isinstance(value, UUID) else None

    async def _user_timezone(self) -> ZoneInfo:
        async with self._database.sessions() as session:
            name = await session.scalar(
                select(AppUserRecord.timezone)
                .where(AppUserRecord.status == "active")
                .order_by(AppUserRecord.created_at)
                .limit(1)
            )
        try:
            return ZoneInfo(name or "Asia/Shanghai")
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("Asia/Shanghai")

    async def _log(
        self,
        policy: HomeAssistantEntityConfig,
        rule: HomeAssistantProactiveRuleConfig,
        now: datetime,
        *,
        passed: bool,
        reason: str | None = None,
        user_id: UUID | None = None,
        conversation_id: UUID | None = None,
        message: str | None = None,
    ) -> None:
        async with self._database.sessions.begin() as session:
            session.add(
                HomeAssistantProactiveLogRecord(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    entity_id=policy.entity_id,
                    rule_id=rule.rule_id,
                    trigger_kind=rule.kind,
                    passed_gate=passed,
                    reason_code=reason,
                    message=message,
                    created_at=now,
                )
            )

    @staticmethod
    def _matches(rule: HomeAssistantProactiveRuleConfig, state: HomeAssistantState | None) -> bool:
        value = state.state.strip().casefold() if state is not None else "unavailable"
        if rule.kind == "device_offline":
            return state is None or value in {"unavailable", "unknown", "offline"}
        if rule.kind == "water_leak":
            return value in {"on", "wet", "true", "1", "detected"}
        if rule.kind == "light_on_too_long":
            return value == "on"
        try:
            numeric = float(value)
        except ValueError:
            return False
        threshold = rule.threshold
        if threshold is None:
            return False
        if rule.kind in {"temperature_high", "humidity_high", "pm25_high"}:
            return numeric > threshold
        if rule.kind in {"temperature_low", "humidity_low"}:
            return numeric < threshold
        return False

    @staticmethod
    def _render_message(
        policy: HomeAssistantEntityConfig,
        rule: HomeAssistantProactiveRuleConfig,
        state: HomeAssistantState | None,
    ) -> str:
        name = policy.display_name
        value = state.state if state is not None else "离线"
        threshold = rule.threshold
        threshold_text = f"{threshold:g}" if threshold is not None else ""
        templates = {
            "water_leak": f"检测到{name}触发了水浸告警，请尽快检查附近是否漏水。",
            "temperature_high": (
                f"{name}当前为 {value}°C，已高于 {threshold_text}°C，建议通风或调低空调温度。"
            ),
            "temperature_low": (
                f"{name}当前为 {value}°C，已低于 {threshold_text}°C，建议注意保暖。"
            ),
            "humidity_high": (
                f"{name}当前湿度为 {value}%，已高于 {threshold_text}%，建议除湿或通风。"
            ),
            "humidity_low": (
                f"{name}当前湿度为 {value}%，已低于 {threshold_text}%，可以考虑加湿。"
            ),
            "pm25_high": (
                f"{name}当前 PM2.5 为 {value}，已高于 {threshold_text}，建议开启空气净化器。"
            ),
            "light_on_too_long": f"{name}已经持续开启较长时间，需要的话可以告诉我关闭它。",
            "device_offline": f"{name}当前不可用，建议检查设备供电和 Home Assistant 连接。",
        }
        return templates[rule.kind]

    @staticmethod
    def _in_quiet_hours(current: time, start_text: str, end_text: str) -> bool:
        start = time.fromisoformat(start_text)
        end = time.fromisoformat(end_text)
        if start == end:
            return False
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _cancel(self, key: tuple[str, str]) -> None:
        task = self._tasks.pop(key, None)
        if task is not None:
            task.cancel()

    def _cancel_entity(self, entity_id: str) -> None:
        for key in tuple(self._tasks):
            if key[0] == entity_id:
                self._cancel(key)


def _state_changed_at(state: HomeAssistantState | None, *, fallback: datetime) -> datetime:
    return state.last_changed if state is not None and state.last_changed is not None else fallback
