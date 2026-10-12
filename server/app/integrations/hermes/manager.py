"""HermesManager：配置驱动 + 健康探测 + 用量/并发闸 + run 提交与守望。

不持有 Hub 权限模型；密钥只在内存（secret_ref 经 EnvSecretProvider 解析）。
用量计数为进程内存实现（重启清零，日限可能被重启放宽——设计 §9 已声明）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import HermesConfig
from app.integrations.hermes.client import (
    HermesClientError,
    HermesHttpClient,
    extract_assistant_text,
    extract_run_error,
    extract_run_output,
    extract_run_status,
    hermes_client_factory,
)
from app.llm.provider import EnvSecretProvider, SecretNotFound

logger = logging.getLogger("app.integrations.hermes")


class HermesManagerError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class HermesHealth:
    status: str  # ok | unreachable | disabled
    version: str | None = None
    tool_execution: str | None = None
    reason_code: str | None = None
    checked_at: str | None = None
    latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class HermesRunOutcome:
    run_id: str
    status: str  # completed | failed | cancelled | stopped | approval_timeout
    output_text: str
    submitted_at: float
    finished_at: float
    waited_for_approval: bool = False


@dataclass(slots=True)
class _UserUsage:
    day: date
    count: int = 0
    active: int = 0


@dataclass
class HermesManagerState:
    """Admin/状态工具可暴露的运行时快照（不含密钥）。"""

    health: HermesHealth = HermesHealth(status="disabled")
    daily_counts: dict[str, int] = field(default_factory=dict)
    active_counts: dict[str, int] = field(default_factory=dict)
    recent_runs: list[dict[str, Any]] = field(default_factory=list)


ClientFactory = Callable[..., HermesHttpClient]


class HermesManager:
    def __init__(
        self,
        config_store: ConfigStore | DatabaseConfigStore,
        *,
        client_factory: ClientFactory = hermes_client_factory,
        secrets: EnvSecretProvider | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config_store = config_store
        self._client_factory = client_factory
        self._secrets = secrets or EnvSecretProvider()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._usage: dict[UUID, _UserUsage] = {}
        self._recent_runs: list[dict[str, Any]] = []
        self._health = HermesHealth(status="disabled")
        self._health_checked_at: float = 0.0
        self._health_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 配置与客户端
    # ------------------------------------------------------------------ #

    async def _snapshot(self) -> Any:
        if isinstance(self._config_store, DatabaseConfigStore):
            return await self._config_store.refresh()
        return self._config_store.current

    async def aconfig(self) -> HermesConfig:
        snapshot = await self._snapshot()
        config: HermesConfig = snapshot.config.hermes
        return config

    def _client(self, config: HermesConfig) -> HermesHttpClient:
        if config.base_url is None:
            raise HermesManagerError("hermes_disabled")
        if config.secret_value is not None:
            api_key = config.secret_value
        elif config.secret_ref is not None:
            try:
                api_key = self._secrets.resolve(config.secret_ref)
            except SecretNotFound as error:
                raise HermesManagerError("hermes_secret_missing") from error
        else:
            raise HermesManagerError("hermes_disabled")
        return self._client_factory(
            str(config.base_url),
            api_key,
            verify_tls=config.verify_tls,
            connect_timeout_seconds=config.connect_timeout_ms / 1_000,
            request_timeout_seconds=config.request_timeout_ms / 1_000,
        )

    # ------------------------------------------------------------------ #
    # 健康探测（TTL 缓存）
    # ------------------------------------------------------------------ #

    async def probe(self, *, force: bool = False) -> HermesHealth:
        config = await self.aconfig()
        if not config.enabled:
            self._health = HermesHealth(status="disabled")
            return self._health
        now = time.monotonic()
        if (
            not force
            and self._health.status == "ok"
            and now - self._health_checked_at < config.health_probe_ttl_seconds
        ):
            return self._health
        async with self._health_lock:
            now = time.monotonic()
            if (
                not force
                and self._health.status == "ok"
                and now - self._health_checked_at < config.health_probe_ttl_seconds
            ):
                return self._health
            started = time.perf_counter()
            try:
                payload = await self._client(config).health()
            except HermesClientError as error:
                self._health = HermesHealth(
                    status="unreachable",
                    reason_code=error.reason_code,
                    checked_at=self._clock().isoformat(),
                    latency_ms=(time.perf_counter() - started) * 1_000,
                )
                return self._health
            version = payload.get("version")
            self._health = HermesHealth(
                status="ok",
                version=str(version) if version else None,
                checked_at=self._clock().isoformat(),
                latency_ms=(time.perf_counter() - started) * 1_000,
            )
            self._health_checked_at = time.monotonic()
            return self._health

    @property
    def health(self) -> HermesHealth:
        return self._health

    async def available_actions(self, user_id: UUID) -> tuple[Any, ...]:
        """RuntimeCapabilityProvider：hermes 启用且探测健康时暴露委派能力。

        让「现实能力边界」提示包含 hermes 服务器任务；能力离线时列表为空，
        模型不得据此提出承诺（与 HA/设备能力同一语义）。
        """
        from app.chat.capabilities import RuntimeActionCapability

        config = await self.aconfig()
        if not config.enabled:
            return ()
        # 未探测过不阻断能力暴露——提交失败会如实回执；仅在明确不可达时收起
        if self._health.status == "unreachable":
            return ()
        return (
            RuntimeActionCapability(
                capability_id="hermes:task",
                label="委派 hermes 服务器任务",
                description=(
                    "把自然语言任务委派给 NAS 上的 hermes 后台执行（终端/文件/浏览器，"
                    "受限用户权限），经确认的行动计划触发（action_id: hermes.task），"
                    "完成后主动汇报；只读状态用 hermes_status 查询"
                ),
            ),
        )

    # ------------------------------------------------------------------ #
    # 用量与并发闸
    # ------------------------------------------------------------------ #

    def _usage_of(self, user_id: UUID) -> _UserUsage:
        today = self._clock().date()
        usage = self._usage.get(user_id)
        if usage is None or usage.day != today:
            usage = _UserUsage(day=today)
            self._usage[user_id] = usage
        return usage

    def check_quota(self, user_id: UUID, config: HermesConfig) -> None:
        usage = self._usage_of(user_id)
        if config.daily_run_limit <= 0:
            raise HermesManagerError("hermes_rate_limited")
        if usage.count >= config.daily_run_limit:
            raise HermesManagerError("hermes_daily_limit_reached")
        if usage.active >= config.max_concurrent_runs:
            raise HermesManagerError("hermes_concurrency_limited")

    def _mark_submitted(self, user_id: UUID) -> None:
        usage = self._usage_of(user_id)
        usage.count += 1
        usage.active += 1

    def _mark_finished(self, user_id: UUID) -> None:
        usage = self._usage_of(user_id)
        usage.active = max(0, usage.active - 1)

    def state(self) -> HermesManagerState:
        today = self._clock().date()
        daily = {
            str(user): usage.count
            for user, usage in self._usage.items()
            if usage.day == today
        }
        active = {
            str(user): usage.active
            for user, usage in self._usage.items()
            if usage.day == today and usage.active > 0
        }
        return HermesManagerState(
            health=self._health,
            daily_counts=daily,
            active_counts=active,
            recent_runs=list(self._recent_runs),
        )

    def _record_run(self, entry: dict[str, Any]) -> None:
        self._recent_runs.append(entry)
        del self._recent_runs[:-20]

    # ------------------------------------------------------------------ #
    # run 生命周期
    # ------------------------------------------------------------------ #

    async def submit_task(
        self, user_id: UUID, instruction: str, *, timeout_seconds: int | None = None
    ) -> str:
        config = await self.aconfig()
        if not config.enabled:
            raise HermesManagerError("hermes_disabled")
        limit = min(
            timeout_seconds or config.run_max_seconds,
            config.run_max_seconds,
        )
        self.check_quota(user_id, config)
        client = self._client(config)
        self._mark_submitted(user_id)
        try:
            run_id = await client.submit_run(instruction)
        except HermesClientError as error:
            self._mark_finished(user_id)
            reason = (
                "hermes_submit_unknown"
                if error.reason_code in {"hermes_timeout", "hermes_submit_unknown"}
                else error.reason_code
            )
            self._record_run(
                {
                    "run_id": None,
                    "user_id": str(user_id),
                    "status": "submit_failed",
                    "reason_code": reason,
                    "instruction_chars": len(instruction),
                }
            )
            raise HermesManagerError(reason) from error
        self._record_run(
            {
                "run_id": run_id,
                "user_id": str(user_id),
                "status": "submitted",
                "instruction_chars": len(instruction),
                "timeout_seconds": limit,
            }
        )
        return run_id

    async def watch_task(
        self,
        user_id: UUID,
        run_id: str,
        *,
        on_approval_pending: Callable[[], Awaitable[None]] | None = None,
        is_cancel_requested: Callable[[], Awaitable[bool]] | None = None,
    ) -> HermesRunOutcome:
        """轮询 run 至终态；审批挂起等待 approval_wait_seconds 后主动 stop。

        取消（is_cancel_requested）在下一个轮询点协作式生效，尽力 stop 远端。
        """
        config = await self.aconfig()
        client = self._client(config)
        started = time.monotonic()
        deadline = started + min(config.run_max_seconds, 3_600)
        approval_notified = False
        approval_deadline: float | None = None
        outcome: HermesRunOutcome | None = None
        try:
            while outcome is None:
                if is_cancel_requested is not None and await is_cancel_requested():
                    await self._best_effort_stop(client, run_id)
                    outcome = HermesRunOutcome(
                        run_id=run_id,
                        status="cancelled",
                        output_text="",
                        submitted_at=started,
                        finished_at=time.monotonic(),
                    )
                    break
                if time.monotonic() >= deadline:
                    await self._best_effort_stop(client, run_id)
                    outcome = HermesRunOutcome(
                        run_id=run_id,
                        status="stopped",
                        output_text="",
                        submitted_at=started,
                        finished_at=time.monotonic(),
                    )
                    break
                try:
                    payload = await client.get_run(run_id)
                except HermesClientError as error:
                    if error.reason_code == "hermes_timeout":
                        continue  # 单次轮询超时不算失败，继续到绝对 deadline
                    raise
                status = extract_run_status(payload)
                if status == "waiting_for_approval":
                    if not approval_notified:
                        approval_notified = True
                        approval_deadline = (
                            time.monotonic() + config.approval_wait_seconds
                            if config.approval_wait_seconds > 0
                            else None
                        )
                        if approval_deadline is None:
                            await self._best_effort_stop(client, run_id)
                            outcome = HermesRunOutcome(
                                run_id=run_id,
                                status="approval_timeout",
                                output_text="",
                                submitted_at=started,
                                finished_at=time.monotonic(),
                                waited_for_approval=True,
                            )
                            break
                        if on_approval_pending is not None:
                            with contextlib.suppress(Exception):
                                await on_approval_pending()
                    elif (
                        approval_deadline is not None
                        and time.monotonic() >= approval_deadline
                    ):
                        await self._best_effort_stop(client, run_id)
                        outcome = HermesRunOutcome(
                            run_id=run_id,
                            status="approval_timeout",
                            output_text="",
                            submitted_at=started,
                            finished_at=time.monotonic(),
                            waited_for_approval=True,
                        )
                        break
                elif status in {"completed", "failed", "cancelled", "stopped"}:
                    output = extract_run_output(payload)
                    if not output and status == "completed":
                        # 0.20.2 实测：completed 状态不含正文，经会话消息取回
                        session_id = payload.get("session_id")
                        if isinstance(session_id, str) and session_id:
                            try:
                                messages = await client.get_session_messages(session_id)
                                output = extract_assistant_text(messages)
                            except HermesClientError:
                                logger.warning(
                                    "hermes run %s output fetch failed", run_id
                                )
                    if not output and status == "failed":
                        output = extract_run_error(payload)
                    outcome = HermesRunOutcome(
                        run_id=run_id,
                        status=status,
                        output_text=output[: config.max_result_bytes],
                        submitted_at=started,
                        finished_at=time.monotonic(),
                        waited_for_approval=approval_notified,
                    )
                    break
                await asyncio.sleep(max(0.5, config.run_poll_interval_seconds))
        finally:
            self._mark_finished(user_id)
        assert outcome is not None
        self._record_run(
            {
                "run_id": run_id,
                "user_id": str(user_id),
                "status": outcome.status,
                "output_chars": len(outcome.output_text),
                "waited_for_approval": outcome.waited_for_approval,
            }
        )
        return outcome

    async def _best_effort_stop(self, client: HermesHttpClient, run_id: str) -> None:
        try:
            await client.stop_run(run_id)
        except HermesClientError:
            logger.warning("hermes stop failed for run %s; outcome stays unknown", run_id)
