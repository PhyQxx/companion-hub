"""SAFE-02 告警状态机：critical 告警的分级升级、确认与恢复。

- L1：本地全通道广播（创建告警时立即投递）；
- L2：confirm_window 无确认后升级，手机推送再提醒一次（push_retry 后重复）；
- ack：用户任一通道回应即终止（v1 入口：聊天"知道了/已处理"意图 + 服务端调用）；
- 恢复：重启后扫描 escalating 告警，按已过窗口补齐升级或到期收尾；
- Timeline：safety.alert_raised / safety.alert_escalated / safety.alert_acked 全量落库。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.db import Database, SafetyAlertRecord
from app.schemas import PrivacyLevel
from app.timeline.models import TimelineActor, TimelineSourceType
from app.timeline.store import TimelineStore

from .store import SafetyAlertStore, SafetyAuthorizationStore, SafetyEscalationLedger

logger = logging.getLogger("app.safety")

Deliver = Callable[..., Awaitable[Any]]
Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


def _aware(value: datetime) -> datetime:
    """SQLite DateTime 返回朴素时间，统一按 UTC 补齐后再与时钟比较。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

# 告警最长生命周期：超过后即使无人确认也收尾为 expired
ALERT_LIFETIME = timedelta(hours=2)
# 聊天确认意图：仅在存在活跃告警时生效，普通对话不受影响
_ACK_PATTERN = re.compile(r"^(知道了|收到|已处理|处理了|没事了|解决了)[。!！.~\s]*$")  # noqa: RUF001


class SafetyAlertService:
    def __init__(
        self,
        database: Database,
        config_store: Any,
        deliver: Deliver,
        *,
        timeline: TimelineStore | None = None,
        mailer: Any | None = None,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._database = database
        self._config_store = config_store
        self._deliver = deliver
        self._timeline = timeline
        self._mailer = mailer
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or asyncio.sleep
        self._store = SafetyAlertStore(database)
        self.authorizations = SafetyAuthorizationStore(database)
        self.ledger = SafetyEscalationLedger(database)
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def resume(self) -> int:
        """重启恢复：escalating 告警按已过窗口补齐升级/到期，返回恢复数量。"""
        resumed = 0
        for record in await self._store.all_escalating():
            self._schedule(record)
            resumed += 1
        if resumed:
            logger.info("safety alert state machine resumed %d alerts", resumed)
        return resumed

    async def stop(self) -> None:
        self._stop.set()
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ------------------------------------------------------------------ #
    # 入口：HA critical 规则命中
    # ------------------------------------------------------------------ #

    async def handle(
        self,
        *,
        user_id: UUID,
        rule_id: str,
        entity_id: str,
        message: str,
        evidence: dict[str, object],
    ) -> SafetyAlertRecord | None:
        """创建（或复用）critical 告警并投递 L1；同实体同规则的活跃告警去重。"""
        existing = await self._store.active_for(
            user_id=user_id, entity_id=entity_id, rule_id=rule_id
        )
        if existing is not None:
            return existing
        now = self._clock()
        record = await self._store.create(
            user_id=user_id,
            rule_id=rule_id,
            entity_id=entity_id,
            message=message,
            evidence=evidence,
            l1_at=now,
            expires_at=now + ALERT_LIFETIME,
        )
        await self._deliver_level(record, level=1)
        await self._index(record, "safety.alert_raised", occurred_at=now)
        self._schedule(record)
        return record

    # ------------------------------------------------------------------ #
    # 确认
    # ------------------------------------------------------------------ #

    async def acknowledge(self, alert_id: UUID, *, source: str = "api") -> bool:
        record = await self._store.mark_acked(alert_id, at=self._clock(), source=source)
        if record is None or record.status != "acknowledged":
            return False
        self._cancel_task(alert_id)
        await self._index(record, "safety.alert_acked", source_suffix="acked")
        return True

    async def handle_user_text(self, text: str, *, user_id: UUID) -> int:
        """聊天确认意图：命中确认短语且有活跃告警时全部确认，返回数量。"""
        if _ACK_PATTERN.match(text.strip()) is None:
            return 0
        count = 0
        for record in await self._store.escalating_for_user(user_id):
            if await self.acknowledge(record.id, source="chat"):
                count += 1
        return count

    # ------------------------------------------------------------------ #
    # 升级循环
    # ------------------------------------------------------------------ #

    def _schedule(self, record: SafetyAlertRecord) -> None:
        if self._stop.is_set() or record.status != "escalating":
            return
        existing = self._tasks.get(record.id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run_escalation(record.id), name=f"safety-alert-{record.id}"
        )
        self._tasks[record.id] = task
        task.add_done_callback(lambda done: self._tasks.pop(record.id, None))

    def _cancel_task(self, alert_id: UUID) -> None:
        task = self._tasks.pop(alert_id, None)
        if task is not None:
            task.cancel()

    async def _run_escalation(self, alert_id: UUID) -> None:
        try:
            # L1 → L2：确认窗口
            await self._advance_after(alert_id, target_level=2, level_at="l1_at")
            # L2 重提醒：推送重试间隔
            await self._advance_after(alert_id, target_level=2, level_at="l2_at", repeat=True)
            # L3：预授权联系人邮件升级（每个告警至多一次）
            await self._escalate_contact(alert_id)
            # 生命周期到期收尾
            record = await self._store.get(alert_id)
            if record is not None and record.status == "escalating":
                remaining = (_aware(record.expires_at) - self._clock()).total_seconds()
                if remaining > 0:
                    await self._sleep(remaining)
                fresh = await self._store.get(alert_id)
                if fresh is not None and fresh.status == "escalating":
                    await self._store.mark_expired(alert_id)
                    logger.info("safety alert expired without ack: %s", alert_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("safety alert escalation failed: %s", alert_id)

    async def _advance_after(
        self,
        alert_id: UUID,
        *,
        target_level: int,
        level_at: str,
        repeat: bool = False,
    ) -> None:
        """等待自 level_at 起的窗口（确认窗口或推送重试），然后升级/重提醒。"""
        config = self._config_store.current.config.safety
        window = (
            config.push_retry_minutes * 60
            if repeat
            else config.confirm_window_seconds
        )
        record = await self._store.get(alert_id)
        if record is None or record.status != "escalating":
            return
        anchor = getattr(record, level_at)
        if anchor is None:
            return
        elapsed = (self._clock() - _aware(anchor)).total_seconds()
        if elapsed < window:
            await self._sleep(window - elapsed)
        fresh = await self._store.get(alert_id)
        if fresh is None or fresh.status != "escalating":
            return
        if not repeat:
            updated = await self._store.mark_escalated(
                alert_id, level=target_level, at=self._clock()
            )
            if updated is None or updated.status != "escalating":
                return
        await self._deliver_level(fresh, level=target_level, repeat=repeat)
        await self._index(
            fresh,
            "safety.alert_escalated",
            occurred_at=self._clock(),
            extra={"level": target_level, "repeat": repeat},
            source_suffix=f"esc-{target_level}-{repeat}",
        )

    # ------------------------------------------------------------------ #
    # L3：预授权联系人升级（docs/07 SAFE 章节 §2：预授权 + 发送前告知用户 + 全程审计）
    # ------------------------------------------------------------------ #

    async def _escalate_contact(self, alert_id: UUID) -> None:
        record = await self._store.get(alert_id)
        if record is None or record.status != "escalating":
            return
        if await self.ledger.contacted(alert_id):
            return
        config = self._config_store.current.config.safety
        if not config.escalation_enabled:
            return
        authorization = await self.authorizations.active_for_user(record.user_id)
        if authorization is None or self._mailer is None:
            # 停在 L2 并告知用户无法升级（一次）
            try:
                await self._deliver(
                    "危急告警仍未确认，且未配置预授权紧急联系人（或邮件未启用），"
                    "已停止在本地提醒。回复「知道了」可结束本次告警。",
                    entity_id=f"safety:{record.entity_id}",
                    rule_id=record.rule_id,
                    trigger_kind="safety.alert",
                    privacy_level=PrivacyLevel.L1,
                    target_user_id=record.user_id,
                    broadcast=True,
                )
            except Exception:
                logger.exception("safety escalation-unavailable notice failed: %s", alert_id)
            return
        # 红线：发送前先告知用户本人（任意通道可当场 ack 中止）
        try:
            await self._deliver(
                f"危急告警长时间未确认，正在通过邮件通知紧急联系人 "
                f"{authorization.contact_name}（{authorization.destination}）。"
                f"如已处理请回复「知道了」。",
                entity_id=f"safety:{record.entity_id}",
                rule_id=record.rule_id,
                trigger_kind="safety.alert",
                privacy_level=PrivacyLevel.L1,
                target_user_id=record.user_id,
                broadcast=True,
            )
        except Exception:
            logger.exception("safety escalation pre-notice failed: %s", alert_id)
        now = self._clock()
        try:
            await self._mailer.send(
                to=[authorization.destination],
                subject="【Aria 紧急告警】家中危急告警长时间未确认",
                body=(
                    f"{record.message}\n\n"
                    f"告警时间：{now.isoformat(timespec='minutes')}\n"
                    "该邮件由 Aria 家庭守护在预先授权后自动发送；"
                    "如为误报请让主人回复「知道了」确认。"
                ),
            )
            status, reason = "sent", None
        except Exception as error:
            status, reason = "failed", type(error).__name__
        await self.ledger.record(
            alert_id=alert_id,
            user_id=record.user_id,
            channel="email",
            destination=authorization.destination,
            status=status,
            reason=reason,
        )
        await self._index(
            record,
            "safety.alert_escalated",
            occurred_at=now,
            extra={"level": 3, "channel": "email", "result": status},
            source_suffix="esc-3-email",
        )
        logger.info(
            "safety alert escalated to contact alert=%s status=%s", alert_id, status
        )

    # ------------------------------------------------------------------ #
    # 投递与 Timeline
    # ------------------------------------------------------------------ #

    async def _deliver_level(
        self, record: SafetyAlertRecord, *, level: int, repeat: bool = False
    ) -> None:
        text = record.message if level == 1 else f"【仍需确认】{record.message}"
        if repeat:
            text = f"【再次提醒】{record.message}"
        try:
            await self._deliver(
                text,
                entity_id=f"safety:{record.entity_id}",
                rule_id=record.rule_id,
                trigger_kind="safety.alert",
                privacy_level=PrivacyLevel.L1,
                target_user_id=record.user_id,
                broadcast=True,
            )
        except Exception:
            logger.exception(
                "safety alert delivery failed alert=%s level=%s", record.id, level
            )

    async def _index(
        self,
        record: SafetyAlertRecord,
        event_type: str,
        *,
        occurred_at: datetime | None = None,
        extra: dict[str, object] | None = None,
        source_suffix: str | None = None,
    ) -> None:
        if self._timeline is None:
            return
        # timeline 对 (source_type, source_id, event_type) 唯一：同一告警的
        # 多次升级/确认必须带可区分后缀，否则唯一约束静默吞掉后续事件
        source_id = str(record.id) + (f":{source_suffix}" if source_suffix else "")
        try:
            await self._timeline.index_custom(
                user_id=record.user_id,
                source_id=source_id,
                source_type=TimelineSourceType.SYSTEM,
                actor=TimelineActor.SYSTEM,
                event_type=event_type,
                title="安全告警",
                summary=record.message[:320],
                occurred_at=occurred_at or self._clock(),
                privacy_level=PrivacyLevel.L1,
                metadata={
                    "alert_id": str(record.id),
                    "rule_id": record.rule_id,
                    "entity_id": record.entity_id,
                    "level": record.level,
                    "status": record.status,
                    **(extra or {}),
                },
            )
        except Exception:
            logger.exception("safety timeline index failed: %s %s", record.id, event_type)
