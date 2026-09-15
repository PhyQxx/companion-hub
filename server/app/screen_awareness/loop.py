"""中枢周期屏幕感知：截屏 → 变化检测 → 视觉分析 → Timeline/记忆/主动话题。

隐私边界：
- 原图走 EphemeralDeviceAssetStore，consume-on-read 即焚，不落盘不落库；
- 只持久化视觉模型产出的摘要文本；
- 设备端硬闸门：TCC 屏幕录制授权、锁屏自动拒绝、隐私暂停一键停止（断开连接）；
- Hub 侧总开关为 config.screen_awareness.enabled，纯配置驱动（用户已明示让渡单次授权）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any, Protocol, cast
from uuid import UUID

from PIL import Image
from sqlalchemy import select

from app.cognition.models import CognitiveDecision, SemanticEvent
from app.db import AppUserRecord
from app.ids import uuid7
from app.memory.consolidation import MemoryIngester
from app.memory.models import (
    MemoryCandidate,
    MemoryOriginKind,
    MemorySourceKind,
    MemorySourceRef,
    MemoryType,
)
from app.perception.models import PerceptionResult
from app.schemas.common import PrivacyLevel
from app.timeline.store import TimelineStore

logger = logging.getLogger("app.screen_awareness")

MONITOR_COMMAND = "screen.monitor"
CAPABILITY = "screen.monitor"
ASSET_TTL_SECONDS = 30
WAIT_TIMEOUT_SECONDS = 31
FAILURE_COOLDOWN = timedelta(minutes=10)
# 感知哈希汉明距离低于该值视为画面未变化
DEFAULT_UNCHANGED_THRESHOLD = 6
# 主动事件在感知管线中的稳定窗口
PROACTIVE_STABLE_SECONDS = 10.0

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "expired", "timed_out"}


class ScreenAwarenessError(Exception):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class MonitorCommandSnapshot(Protocol):
    id: UUID
    status: str
    reason_code: str | None
    result_meta: dict[str, Any] | None


class MonitorAsset(Protocol):
    data: bytes


class MonitorAssetStore(Protocol):
    async def consume(
        self, asset_id: UUID, *, owner_user_id: UUID, command_id: UUID
    ) -> MonitorAsset: ...


class DeviceGateway(Protocol):
    assets: MonitorAssetStore

    async def issue(
        self,
        *,
        device_id: UUID,
        command: str,
        args: dict[str, Any],
        idempotency_key: str,
        ttl_seconds: int,
    ) -> MonitorCommandSnapshot: ...

    async def wait_for_terminal(
        self, command_id: UUID, *, timeout_seconds: float
    ) -> MonitorCommandSnapshot: ...


class MonitorDevice(Protocol):
    id: UUID


class ScreenTargetResolver(Protocol):
    async def resolve(
        self, *, owner_user_id: UUID, target: UUID | str | None, capability: str
    ) -> MonitorDevice: ...


class VisionResult(Protocol):
    text: str


class VisionAnalyzer(Protocol):
    async def analyze(
        self, *, data: bytes, media_type: str, prompt: str, privacy_level: PrivacyLevel
    ) -> VisionResult: ...


@dataclass(frozen=True, slots=True)
class ScreenAnalysis:
    summary: str
    notable: bool
    memory_worthy: bool
    topic: str | None


@dataclass(slots=True)
class DisplayState:
    last_hash: int | None = None
    last_analyzed_at: datetime | None = None
    last_summary: str | None = None
    consecutive_failures: int = 0
    disabled_until: datetime | None = None


@dataclass(slots=True)
class LoopState:
    running: bool = False
    cycles: int = 0
    last_tick_at: datetime | None = None
    last_error: str | None = None
    displays: dict[int, DisplayState] = field(default_factory=dict)


def perceptual_hash(data: bytes) -> int:
    """64 位平均感知哈希：8x8 灰度均值二值化。"""
    image = Image.open(BytesIO(data)).convert("L").resize((8, 8))
    pixels = cast(list[int], image.get_flattened_data())
    average = sum(pixels) / len(pixels)
    return sum(1 << index for index, value in enumerate(pixels) if value > average)


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def parse_analysis(text: str) -> ScreenAnalysis:
    """宽松解析视觉模型的 JSON 输出；解析失败时把全文当摘要、视为不值得记忆。"""
    payload: dict[str, Any] = {}
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        with contextlib.suppress(ValueError):
            payload = json.loads(text[start : end + 1])
    summary = str(payload.get("summary") or "").strip() or text.strip()[:400]
    return ScreenAnalysis(
        summary=summary[:400],
        notable=bool(payload.get("notable")),
        memory_worthy=bool(payload.get("memory_worthy")),
        topic=str(payload.get("topic") or "").strip()[:200] or None,
    )


def analysis_prompt(base_prompt: str) -> str:
    return (
        f"{base_prompt}\n"
        "只输出 JSON 对象，字段："
        '{"summary": "不超过120字的画面内容概括", '
        '"notable": 布尔, 是否有值得主动告诉用户的新情况(日程提醒/消息/异常等), '
        '"memory_worthy": 布尔, 是否值得作为长期记忆保存(稳定事实/重要活动, 忽略日常琐碎), '
        '"topic": notable 时的一句话话题开头, 否则 null}'
    )


__all__ = [
    "ScreenAwarenessAnalyzer",
    "ScreenAwarenessError",
    "ScreenAwarenessGateway",
    "ScreenAwarenessLoop",
    "ScreenAwarenessResolver",
    "hamming_distance",
    "parse_analysis",
    "perceptual_hash",
]

ProactiveDeliver = Callable[..., Awaitable[Any]]
PerceptionHandler = Callable[[SemanticEvent, Any], Awaitable[None]]

# main.py 装配点 cast 用的协议别名
ScreenAwarenessResolver = ScreenTargetResolver
ScreenAwarenessGateway = DeviceGateway
ScreenAwarenessAnalyzer = VisionAnalyzer


class ConfigView(Protocol):
    """只需要 .current.config 的最小配置源协议（ConfigStore/DatabaseConfigStore 均满足）。"""

    @property
    def current(self) -> Any: ...


class ScreenAwarenessLoop:
    """照 ConfigWatcher 模式：asyncio task + stop event；每 tick 重读配置支持热更新。"""

    def __init__(
        self,
        *,
        config_store: ConfigView,
        database: Any,
        resolver: ScreenTargetResolver,
        gateway: DeviceGateway,
        analyzer: VisionAnalyzer,
        timeline: TimelineStore,
        memory_ingester: MemoryIngester | None = None,
        perception_pipeline: Any = None,
        proactive_deliver: ProactiveDeliver | None = None,
        cognitive_cycle: Any = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._config_store = config_store
        self._database = database
        self._resolver = resolver
        self._gateway = gateway
        self._analyzer = analyzer
        self._timeline = timeline
        self._memory_ingester = memory_ingester
        self._perception = perception_pipeline
        self._proactive_deliver = proactive_deliver
        self._cycle = cognitive_cycle
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or asyncio.sleep
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()
        self.state = LoopState()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self.state.running = True
        self._task = asyncio.create_task(self._run(), name="aria-screen-awareness")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        self.state.running = False
        for background in list(self._background):
            background.cancel()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            config = self._config_store.current.config.screen_awareness
            interval = float(config.interval_seconds)
            try:
                await self._tick(config)
            except Exception as error:
                self.state.last_error = f"{type(error).__name__}: {error}"
                logger.warning("screen awareness tick failed: %s", error, exc_info=True)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)

    async def _tick(self, config: Any) -> None:
        self.state.cycles += 1
        self.state.last_tick_at = self._clock()
        if not config.enabled:
            return
        owner = await self._resolve_owner()
        if owner is None:
            return
        device = await self._resolve_device(owner)
        if device is None:
            return
        for display in config.displays:
            if self._stop.is_set():
                return
            await self._capture_display(config, owner, device, int(display))

    async def _resolve_owner(self) -> UUID | None:
        async with self._database.sessions() as session:
            owner = await session.scalar(
                select(AppUserRecord.id).order_by(AppUserRecord.created_at)
            )
        return UUID(str(owner)) if owner is not None else None

    async def _resolve_device(self, owner: UUID) -> MonitorDevice | None:
        from app.devices import (
            DeviceTargetAmbiguous,
            DeviceTargetNotFound,
            DeviceTargetUnavailable,
        )

        try:
            return await self._resolver.resolve(
                owner_user_id=owner, target=None, capability=CAPABILITY
            )
        except (DeviceTargetAmbiguous, DeviceTargetNotFound, DeviceTargetUnavailable):
            return None

    async def _capture_display(
        self, config: Any, owner: UUID, device: MonitorDevice, display: int
    ) -> None:
        state = self.state.displays.setdefault(display, DisplayState())
        now = self._clock()
        if state.disabled_until is not None and now < state.disabled_until:
            return
        try:
            image = await self._capture_bytes(device, display, owner)
        except ScreenAwarenessError as error:
            state.consecutive_failures += 1
            if state.consecutive_failures >= 3:
                # 连续失败进入 10 分钟冷却，避免对不存在的显示器反复空转
                state.disabled_until = now + FAILURE_COOLDOWN
                state.consecutive_failures = 0
                logger.warning(
                    "screen awareness display %s disabled for 10min: %s", display, error.reason_code
                )
            return
        state.consecutive_failures = 0
        state.disabled_until = None

        digest = perceptual_hash(image)
        threshold = config.unchanged_skip_threshold or DEFAULT_UNCHANGED_THRESHOLD
        if (
            state.last_hash is not None
            and hamming_distance(state.last_hash, digest) <= threshold
        ):
            return
        state.last_hash = digest

        analysis = await self._analyze(config, image)
        state.last_analyzed_at = self._clock()
        state.last_summary = analysis.summary

        observation_id = uuid7()
        await self._timeline.index_screen_observation(
            user_id=owner,
            observation_id=observation_id,
            display=display,
            summary=analysis.summary,
            privacy_level=PrivacyLevel.L1,
            occurred_at=now,
            metadata={"hash": f"{digest:016x}", "device_id": str(device.id)},
        )
        if analysis.memory_worthy and config.memory_enabled and self._memory_ingester is not None:
            with contextlib.suppress(Exception):
                await self._memory_ingester.ingest(
                    MemoryCandidate(
                        type=MemoryType.EPISODIC,
                        origin_kind=MemoryOriginKind.SYSTEM_EVENT,
                        content=f"[屏幕观察 · 显示器 {display}] {analysis.summary}"[:2_000],
                        privacy_level=PrivacyLevel.L1,
                        sources=[
                            MemorySourceRef(
                                source_kind=MemorySourceKind.EVENT,
                                source_id=str(observation_id),
                                excerpt=analysis.summary[:2_000],
                            )
                        ],
                        importance=0.3,
                        extractor_version="screen-v1",
                    ),
                    user_id=owner,
                    actor="screen-awareness",
                )
        if analysis.notable and config.proactive_enabled:
            self._submit_proactive(owner, observation_id, digest, analysis, display)

    async def _capture_bytes(
        self, device: MonitorDevice, display: int, owner: UUID
    ) -> bytes:
        command_args: dict[str, Any] = (
            {"target": "main_display"}
            if display == 1
            else {"target": "display", "display_index": display}
        )
        command = await self._gateway.issue(
            device_id=device.id,
            command=MONITOR_COMMAND,
            args=command_args,
            idempotency_key=f"screen-monitor-{uuid7()}",
            ttl_seconds=ASSET_TTL_SECONDS,
        )
        if command.status not in TERMINAL_STATUSES:
            command = await self._gateway.wait_for_terminal(
                command.id, timeout_seconds=WAIT_TIMEOUT_SECONDS
            )
        if command.status != "succeeded" or command.result_meta is None:
            reason = command.reason_code or f"command_{command.status}"
            raise ScreenAwarenessError(reason)
        asset = await self._gateway.assets.consume(
            UUID(str(command.result_meta["asset_id"])),
            owner_user_id=owner,
            command_id=command.id,
        )
        return asset.data

    async def _analyze(self, config: Any, image: bytes) -> ScreenAnalysis:
        result = await self._analyzer.analyze(
            data=image,
            media_type="image/png",
            prompt=analysis_prompt(config.analysis_prompt),
            privacy_level=PrivacyLevel.L1,
        )
        return parse_analysis(result.text)

    def _submit_proactive(
        self,
        owner: UUID,
        observation_id: UUID,
        digest: int,
        analysis: ScreenAnalysis,
        display: int,
    ) -> None:
        now = self._clock()
        event = SemanticEvent(
            event_id=uuid7(),
            user_id=owner,
            kind="screen.observed",
            source_kind="screen",
            dedupe_key=f"screen:{digest:016x}",
            summary=f"[显示器 {display}] {analysis.summary}"[:500],
            occurred_at=now,
            privacy_level=PrivacyLevel.L1,
            confidence=0.8,
            evidence_ids=[str(observation_id)],
            attributes={
                "message": analysis.topic or analysis.summary,
                "display": display,
                # 视觉判定 notable 的自带显著性：让注意力引擎按内容重要性评分，
                # 否则 screen.observed 只拿默认基础分永远到不了 deliberation 阈值
                "salience": 0.75,
            },
            expires_at=now + timedelta(minutes=5),
        )

        async def handler(event: SemanticEvent, result: PerceptionResult) -> None:
            await self._deliver_event(event, result.decision)

        if self._perception is not None:
            self._perception.submit(
                event,
                stable_for_seconds=PROACTIVE_STABLE_SECONDS,
                validate=None,
                handler=handler,
            )
        else:
            task = asyncio.create_task(self._evaluate_direct(event))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _deliver_event(
        self, event: SemanticEvent, decision: CognitiveDecision | None
    ) -> None:
        import logging

        diag = logging.getLogger("aria.diag")
        if not diag.handlers:
            diag.addHandler(logging.FileHandler("/tmp/aria_deliver_diag.log"))
        diag.warning("DIAG screen._deliver_event kind=%s decision=%s fn=%s", event.kind, decision and decision.decision, self._proactive_deliver)
        if self._proactive_deliver is None or decision is None:
            return
        if decision.decision not in {"inform", "suggest", "ask", "escalate"}:
            return
        try:
            result = await self._proactive_deliver(
                str(event.attributes.get("message", event.summary)),
                entity_id=f"display:{event.attributes.get('display', 1)}",
                rule_id=f"perception_{event.kind}",
                trigger_kind=event.kind,
                privacy_level=event.privacy_level,
                cognitive_decision=decision,
                target_user_id=event.user_id,
            )
            diag.warning("DIAG screen deliver result=%s", result)
        except Exception:
            diag.exception("DIAG screen deliver raised")

    async def _evaluate_direct(self, event: SemanticEvent) -> None:
        """无感知管线时的降级路径：直接走认知循环判定。"""
        if self._cycle is None:
            await self._deliver_event(event, None)
            return
        with contextlib.suppress(Exception):
            decision = await self._cycle.evaluate(event)
            await self._deliver_event(event, decision)
