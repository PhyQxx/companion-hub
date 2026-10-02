"""RPT 每日自体检（docs/09 §7）：路由连通/设备心跳/备份落盘，异常才说话。

透明度汇报回答「你为什么打扰我」；自体检是它的镜像面——系统每天
主动检查一次自身运行条件，只有发现异常才经主动通道说话，全部健康
则保持安静。检查全部确定性：模型路由做一次最小补全探活，设备心跳
与备份新鲜度只读数据库与目录，不携带任何用户内容。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.db import AppUserRecord, Database
from app.devices.service import DeviceRegistry
from app.ids import uuid7
from app.jobs import JobEngine
from app.llm import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.llm.factory import build_router
from app.llm.provider import EnvSecretProvider
from app.schemas.common import PrivacyLevel

logger = logging.getLogger("app.observability.selfcheck")

BACKUP_STALE_AFTER = timedelta(hours=26)
DEVICE_STALE_AFTER = timedelta(hours=24)
DEVICE_RECENT_WINDOW = timedelta(days=7)
CHECK_TIME_ENV = "ARIA_SELF_CHECK_TIME"
DEFAULT_CHECK_TIME = dt_time(10, 0)


@dataclass(frozen=True, slots=True)
class SelfCheckFinding:
    check: str
    detail: str

    def render(self) -> str:
        return f"- {self.check}：{self.detail}"


class RouteBackend(Protocol):
    """路由探活所需的最小补全接口。"""

    async def complete(self, request: CompletionRequest) -> CompletionResult: ...


class SelfCheckDeliverer(Protocol):
    async def __call__(
        self,
        text: str,
        *,
        entity_id: str,
        rule_id: str,
        trigger_kind: str,
        privacy_level: PrivacyLevel,
        target_user_id: UUID,
    ) -> object | None: ...


class DailySelfCheckScheduler:
    """每日一次确定性自体检；异常才经主动通道汇报，全部健康保持安静。"""

    def __init__(
        self,
        database: Database,
        *,
        config_store: ConfigStore | DatabaseConfigStore | None = None,
        device_registry: DeviceRegistry | None = None,
        backup_dir: Path | None = None,
        timezone_name: str = "Asia/Shanghai",
        check_time: dt_time | None = None,
        interval_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        deliverer: SelfCheckDeliverer | None = None,
        router_builder: Callable[[HubConfig], RouteBackend] | None = None,
    ) -> None:
        self._database = database
        self._config_store = config_store
        self._device_registry = device_registry
        self._backup_dir = backup_dir
        self._tz = ZoneInfo(timezone_name)
        self._check_time = check_time or self._parse_time(os.getenv(CHECK_TIME_ENV, "10:00"))
        self._interval = interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or asyncio.sleep
        self._deliverer = deliverer
        self._router_builder = router_builder
        self._last_check_date: str | None = None
        self._jobs = JobEngine(database)
        self._worker_id = f"self-check-{uuid7()}"
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @staticmethod
    def _parse_time(raw: str) -> dt_time:
        hour, minute = raw.split(":", 1)
        return dt_time(int(hour), int(minute))

    def set_deliverer(self, deliverer: SelfCheckDeliverer) -> None:
        """主动投递栈装配后期注入（ProactiveDeliveryService.deliver）。"""
        self._deliverer = deliverer

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="aria-daily-selfcheck")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        # 首个 tick 前等待一个完整间隔，与既有调度器节奏一致
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("daily self check tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    async def run_once(self, *, now: datetime | None = None) -> list[SelfCheckFinding]:
        """到达检查时刻后体检一次（每日至多一次）；返回本轮发现。"""
        moment = now or self._clock()
        local = moment.astimezone(self._tz)
        if (local.hour, local.minute) < (self._check_time.hour, self._check_time.minute):
            return []
        today = local.date().isoformat()
        if self._last_check_date == today:
            return []
        slot = datetime.combine(local.date(), self._check_time, self._tz).astimezone(UTC)
        job = await self._jobs.submit_scheduled(
            "system.self_check",
            {},
            schedule_id=f"self-check:{self._tz.key}",
            scheduled_slot=slot,
            owner="system-self-check",
            resource_class="self-check",
        )
        if job.status in {"succeeded", "failed", "cancelled"}:
            self._last_check_date = today
            return []
        await self._jobs.expire_stale_leases(resource_class="self-check")
        await self._jobs.release_ready_retries(resource_class="self-check")
        claimed = await self._jobs.claim(
            self._worker_id, resource_class="self-check", job_id=job.id
        )
        if claimed is None:
            return []
        step = await self._jobs.start_step(
            job.id, "check", worker_id=self._worker_id, claim_version=claimed.attempts
        )
        try:
            findings = await self._perform_check(moment)
            await self._jobs.complete_step(
                step, worker_id=self._worker_id, claim_version=claimed.attempts
            )
            if await self._jobs.succeed(
                job.id, worker_id=self._worker_id, claim_version=claimed.attempts
            ):
                self._last_check_date = today
            return findings
        except Exception:
            await self._jobs.fail_step(step, error_code="self_check_failed")
            raise

    async def _perform_check(self, moment: datetime) -> list[SelfCheckFinding]:
        findings: list[SelfCheckFinding] = []
        findings.extend(await self._check_route())
        findings.extend(await self._check_devices())
        findings.extend(self._check_backups(moment))
        if findings and self._deliverer is not None:
            detail_lines = "\n".join(item.render() for item in findings)
            text = f"每日自体检发现 {len(findings)} 项异常：\n{detail_lines}"
            for user_id in await self._active_user_ids():
                try:
                    await self._deliverer(
                        text,
                        entity_id="selfcheck",
                        rule_id="self_check",
                        trigger_kind="rpt.self_check",
                        privacy_level=PrivacyLevel.L1,
                        target_user_id=user_id,
                    )
                except Exception:
                    logger.warning("self check delivery failed for %s", user_id, exc_info=True)
        return findings

    # ---------------------------------------------------------------- #
    # 检查项
    # ---------------------------------------------------------------- #

    async def _check_route(self) -> list[SelfCheckFinding]:
        if self._config_store is None:
            return []
        try:
            snapshot = (
                await self._config_store.refresh()
                if isinstance(self._config_store, DatabaseConfigStore)
                else self._config_store.current
            )
            builder = self._router_builder or (
                lambda config: build_router(config, EnvSecretProvider())
            )
            backend = builder(snapshot.config)
            await backend.complete(
                CompletionRequest(
                    trace_id=uuid7(),
                    messages=[LLMMessage(role="user", content="ping")],
                    privacy_level=PrivacyLevel.L0,
                    route=LLMRoute.UTILITY,
                    temperature=0,
                    max_tokens=1,
                )
            )
        except Exception:
            logger.info("self check route probe failed", exc_info=True)
            return [
                SelfCheckFinding(
                    check="模型路由",
                    detail="连通性探活失败，对话可能不可用；请检查模型配置或网关。",
                )
            ]
        return []

    async def _check_devices(self) -> list[SelfCheckFinding]:
        if self._device_registry is None:
            return []
        try:
            devices = await self._device_registry.list_devices()
        except Exception:
            logger.warning("self check device listing failed", exc_info=True)
            return []
        now = self._clock()
        findings: list[SelfCheckFinding] = []
        for device in devices:
            if getattr(device, "revoked_at", None) is not None:
                continue
            last_seen = getattr(device, "last_seen_at", None)
            if last_seen is None:
                continue
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=UTC)
            silence = now - last_seen
            if DEVICE_STALE_AFTER < silence <= DEVICE_RECENT_WINDOW:
                days = max(1, int(silence.total_seconds() // 86_400))
                findings.append(
                    SelfCheckFinding(
                        check="设备心跳",
                        detail=f"设备「{device.name}」已约 {days} 天未心跳，可能离线。",
                    )
                )
        return findings

    def _check_backups(self, now: datetime) -> list[SelfCheckFinding]:
        directory = self._backup_dir
        if directory is None:
            directory = Path(os.getenv("ARIA_BACKUP_DIR", "backups"))
        try:
            dumps = (
                sorted(
                    (item for item in directory.glob("aria_*.dump") if item.is_file()),
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
                if directory.is_dir()
                else []
            )
        except OSError:
            return []
        if not dumps:
            # 目录不存在或从未产出备份：视为未启用备份，不制造噪音
            return []
        newest_age = now - datetime.fromtimestamp(dumps[0].stat().st_mtime, tz=UTC)
        if newest_age > BACKUP_STALE_AFTER:
            stale_hours = int(newest_age.total_seconds() // 3_600)
            return [
                SelfCheckFinding(
                    check="备份落盘",
                    detail=f"最新数据库备份已 {stale_hours} 小时未更新，超过 26 小时阈值。",
                )
            ]
        return []

    async def _active_user_ids(self) -> list[UUID]:
        async with self._database.sessions() as session:
            records = (
                await session.scalars(
                    select(AppUserRecord.id)
                    .where(AppUserRecord.status == "active")
                    .order_by(AppUserRecord.created_at)
                )
            ).all()
            return list(records)
