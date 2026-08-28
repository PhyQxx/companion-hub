from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from typing import Any
from uuid import UUID

import pytest
from PIL import Image

from app.cognition.models import CognitiveDecision, DecisionKind, Urgency
from app.config.models import HubConfig
from app.db import Base, Database, create_database
from app.ids import uuid7
from app.memory.models import MemoryOriginKind, MemoryType
from app.perception.models import PerceptionDisposition, PerceptionResult
from app.schemas.common import PrivacyLevel
from app.screen_awareness import (
    ScreenAwarenessError,
    ScreenAwarenessLoop,
    hamming_distance,
    parse_analysis,
    perceptual_hash,
)
from app.timeline.store import TimelineStore

PNG_GRAY = b"\x89PNG\r\n\x1a\nplaceholder"


def make_png(color: tuple[int, int, int]) -> bytes:
    """带横向渐变纹理的图：纯色图所有像素等于均值，感知哈希恒为 0。"""
    image = Image.new("RGB", (64, 48), color)
    for x in range(64):
        for y in range(0, 48, 8):
            image.putpixel((x, y), (x * 4 % 256, y * 5 % 256, (x + y) * 2 % 256))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


@dataclass
class FakeCommand:
    id: UUID
    status: str = "succeeded"
    reason_code: str | None = None
    result_meta: dict[str, Any] | None = field(default_factory=lambda: {"asset_id": str(uuid7())})


@dataclass
class FakeAsset:
    data: bytes


class FakeAssetStore:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.consumed: list[str] = []

    async def consume(self, asset_id: Any, *, owner_user_id: UUID, command_id: UUID) -> FakeAsset:
        self.consumed.append(str(asset_id))
        return FakeAsset(data=self.data)


class FakeGateway:
    def __init__(self, data: bytes, *, fail: bool = False) -> None:
        self.assets = FakeAssetStore(data)
        self.fail = fail
        self.issued: list[dict[str, Any]] = []

    async def issue(self, **kwargs: Any) -> FakeCommand:
        self.issued.append(kwargs)
        if self.fail:
            return FakeCommand(id=uuid7(), status="failed", reason_code="screen_locked")
        return FakeCommand(id=uuid7())

    async def wait_for_terminal(self, command_id: UUID, *, timeout_seconds: float) -> FakeCommand:
        raise AssertionError("already-terminal command must not wait")


class FakeResolver:
    def __init__(self, device: Any = None) -> None:
        self.device = device or type("D", (), {"id": uuid7()})()

    async def resolve(self, **_: Any) -> Any:
        return self.device


class FakeAnalyzer:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def analyze(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return type("R", (), {"text": self.payload})()


class FakeConfigStore:
    def __init__(self, config: HubConfig) -> None:
        self.current = type("S", (), {"config": config})()


@dataclass
class FakeSessionResult:
    scalar: Any


class FakeSessions:
    def __init__(self, owner: UUID | None) -> None:
        self.owner = owner

    async def __aenter__(self) -> Any:
        return type("S", (), {"scalar": lambda self_, *_: self.owner})()

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakeDatabase:
    def __init__(self, owner: UUID) -> None:
        self.owner = owner
        self.sessions = lambda: FakeSessions(owner)


class FakeMemoryIngester:
    def __init__(self) -> None:
        self.candidates: list[Any] = []

    async def ingest(self, candidate: Any, *, user_id: UUID, actor: str = "extractor") -> Any:
        self.candidates.append((candidate, user_id, actor))
        return type("O", (), {"decision": "created"})()


class FakePerception:
    def __init__(self) -> None:
        self.submitted: list[tuple[Any, float, Any]] = []

    def submit(
        self,
        event: Any,
        *,
        stable_for_seconds: float = 0,
        validate: Any = None,
        handler: Any = None,
    ) -> None:
        self.submitted.append((event, stable_for_seconds, handler))


def make_config(**overrides: Any) -> HubConfig:
    candidate = {
        "models": {
            "cloud": {
                "enabled": True,
                "kind": "text",
                "provider": "openai_compatible",
                "model": "cloud-model",
                "base_url": "https://cloud.example/v1",
                "runs_local": False,
                "max_privacy_level": "L1",
            },
            "local": {
                "enabled": True,
                "kind": "text",
                "provider": "openai_compatible",
                "model": "local-model",
                "base_url": "http://127.0.0.1:1234/v1",
                "runs_local": True,
                "max_privacy_level": "L2",
                "supports_tool_calling": True,
            },
        },
        "routes": {
            "dialogue": {"primary": "cloud"},
            "utility": {"primary": "cloud"},
            "private": {"primary": "local"},
        },
    }
    candidate["screen_awareness"] = {
        "enabled": True,
        "interval_seconds": 15,
        "displays": [1, 2],
        **overrides,
    }
    return HubConfig.model_validate(candidate)


@pytest.fixture
def database() -> Database:
    return create_database("sqlite+aiosqlite:///:memory:")


@pytest.fixture
async def migrated(database: Database) -> Database:
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return database


def make_loop(
    migrated: Database,
    config: HubConfig,
    gateway: Any,
    analyzer: Any,
    *,
    memory: Any = None,
    perception: Any = None,
    image: bytes | None = None,
) -> tuple[ScreenAwarenessLoop, UUID]:
    owner = uuid7()
    timeline = TimelineStore(migrated)
    loop = ScreenAwarenessLoop(
        config_store=FakeConfigStore(config),
        database=FakeDatabase(owner),
        resolver=FakeResolver(),
        gateway=gateway,
        analyzer=analyzer,
        timeline=timeline,
        memory_ingester=memory,
        perception_pipeline=perception,
        clock=lambda: datetime.now(UTC),
        sleeper=asyncio.sleep,
    )
    # 测试桩替换真实设备解析（fake DB 直接返回固定 owner）

    async def resolve_owner() -> UUID:
        return owner

    loop._resolve_owner = resolve_owner  # type: ignore[method-assign]
    return loop, owner


ANALYSIS_JSON = (
    '{"summary": "用户在写代码，屏幕上是终端", "notable": true, '
    '"memory_worthy": true, "topic": "我注意到你在写代码"}'
)


async def test_perceptual_hash_stability_and_distance() -> None:
    image_a = make_png((200, 200, 200))
    image_b = make_png((20, 20, 20))
    hash_a = perceptual_hash(image_a)
    assert hash_a == perceptual_hash(image_a)
    assert hamming_distance(hash_a, perceptual_hash(image_b)) > 6
    # 同图不同编码字节（重压缩）哈希应保持接近
    reencoded = make_png((200, 200, 200))
    assert hamming_distance(hash_a, perceptual_hash(reencoded)) <= 6


def test_parse_analysis_lenient() -> None:
    parsed = parse_analysis(
        '前言 {"summary": "写代码", "notable": true, "memory_worthy": false, "topic": null} 后记'
    )
    assert parsed.summary == "写代码"
    assert parsed.notable is True
    assert parsed.memory_worthy is False
    fallback = parse_analysis("这不是 JSON 输出")
    assert fallback.summary == "这不是 JSON 输出"
    assert fallback.notable is False


async def test_config_disabled_loop_is_noop(migrated: Database) -> None:
    config = make_config(enabled=False)
    gateway = FakeGateway(make_png((10, 10, 10)))
    analyzer = FakeAnalyzer(ANALYSIS_JSON)
    loop, _owner = make_loop(migrated, config, gateway, analyzer)

    await loop._tick(config.screen_awareness)
    assert gateway.issued == []
    assert analyzer.calls == []


async def test_changed_screen_writes_timeline_memory_and_proactive(migrated: Database) -> None:
    memory = FakeMemoryIngester()
    perception = FakePerception()
    gateway = FakeGateway(make_png((100, 150, 200)))
    analyzer = FakeAnalyzer(ANALYSIS_JSON)
    config = make_config()
    loop, owner = make_loop(
        migrated, config, gateway, analyzer, memory=memory, perception=perception
    )

    await loop._tick(config.screen_awareness)

    # 两块屏各发一次命令
    assert [item["command"] for item in gateway.issued] == [
        "screen.monitor",
        "screen.monitor",
    ]
    assert gateway.issued[0]["args"] == {"target": "main_display"}
    assert gateway.issued[1]["args"] == {"target": "display", "display_index": 2}
    # 视觉分析每屏一次
    assert len(analyzer.calls) == 2
    # Timeline 全量记录
    async with migrated.sessions() as session:
        from sqlalchemy import select

        from app.db import TimelineEventRecord

        rows = list(
            await session.scalars(
                select(TimelineEventRecord).where(
                    TimelineEventRecord.event_type == "screen.observed"
                )
            )
        )
    assert len(rows) == 2
    assert {row.metadata_json["display"] for row in rows} == {1, 2}
    # memory_worthy=true → 记忆沉淀
    assert len(memory.candidates) == 2
    candidate = memory.candidates[0][0]
    assert candidate.type == MemoryType.EPISODIC
    assert candidate.origin_kind == MemoryOriginKind.SYSTEM_EVENT
    assert candidate.extractor_version == "screen-v1"
    assert memory.candidates[0][1] == owner
    # notable=true → 主动事件进感知管线（带稳定窗与处理器）
    assert len(perception.submitted) == 2
    event, stable, handler = perception.submitted[0]
    assert event.kind == "screen.observed"
    assert event.source_kind == "screen"
    assert event.evidence_ids
    assert stable == pytest.approx(10.0)
    assert handler is not None


async def test_perception_handler_unwraps_cognitive_decision(migrated: Database) -> None:
    perception = FakePerception()
    delivered: list[dict[str, Any]] = []

    async def proactive_deliver(message: str, **kwargs: Any) -> None:
        delivered.append({"message": message, **kwargs})

    loop, owner = make_loop(
        migrated,
        make_config(displays=[1]),
        FakeGateway(make_png((100, 150, 200))),
        FakeAnalyzer(ANALYSIS_JSON),
        perception=perception,
    )
    loop._proactive_deliver = proactive_deliver
    await loop._tick(make_config(displays=[1]).screen_awareness)
    event, _stable, handler = perception.submitted[0]
    now = datetime.now(UTC)
    decision = CognitiveDecision(
        id=uuid7(),
        event_id=event.event_id,
        user_id=owner,
        trigger_kind=event.kind,
        decision=DecisionKind.SUGGEST,
        reason_codes=["screen_notable"],
        evidence_ids=event.evidence_ids,
        confidence=0.8,
        urgency=Urgency.NORMAL,
        attention_score=0.75,
        policy_version="test",
        message="可以提供帮助",
        created_at=now,
    )
    result = PerceptionResult(
        event_id=event.event_id,
        disposition=PerceptionDisposition.PROCESSED,
        decision=decision,
    )

    await handler(event, result)

    assert len(delivered) == 1
    assert delivered[0]["cognitive_decision"] is decision


async def test_unchanged_screen_skips_analysis(migrated: Database) -> None:
    gateway = FakeGateway(make_png((90, 90, 90)))
    analyzer = FakeAnalyzer(ANALYSIS_JSON)
    config = make_config()
    loop, _owner = make_loop(migrated, config, gateway, analyzer)

    await loop._tick(config.screen_awareness)
    assert len(analyzer.calls) == 2
    # 画面不变时第二轮仍会截屏（用于变化检测），但不触发分析
    await loop._tick(config.screen_awareness)
    assert len(analyzer.calls) == 2
    assert len(gateway.issued) == 4


async def test_failed_capture_enters_cooldown(migrated: Database) -> None:
    gateway = FakeGateway(PNG_GRAY, fail=True)
    analyzer = FakeAnalyzer(ANALYSIS_JSON)
    config = make_config()
    loop, _owner = make_loop(migrated, config, gateway, analyzer)

    await loop._tick(config.screen_awareness)
    await loop._tick(config.screen_awareness)
    await loop._tick(config.screen_awareness)
    display_state = loop.state.displays[1]
    assert display_state.disabled_until is not None
    assert analyzer.calls == []
    # 冷却期间不再发命令
    issued_after_cooldown = len(gateway.issued)
    await loop._tick(config.screen_awareness)
    assert len(gateway.issued) == issued_after_cooldown


async def test_timeline_l3_and_empty_summary_rejected(migrated: Database) -> None:
    timeline = TimelineStore(migrated)
    assert (
        await timeline.index_screen_observation(
            user_id=uuid7(),
            observation_id=uuid7(),
            display=1,
            summary="",
            privacy_level=PrivacyLevel.L1,
            occurred_at=datetime.now(UTC),
        )
        is None
    )
    assert (
        await timeline.index_screen_observation(
            user_id=uuid7(),
            observation_id=uuid7(),
            display=1,
            summary="密测",
            privacy_level=PrivacyLevel.L3,
            occurred_at=datetime.now(UTC),
        )
        is None
    )


async def test_start_stop_lifecycle(migrated: Database) -> None:
    config = make_config()
    loop, _owner = make_loop(migrated, config, FakeGateway(make_png((5, 5, 5))), FakeAnalyzer("{}"))
    loop.start()
    assert loop.state.running is True
    await asyncio.sleep(0.01)
    await loop.stop()
    assert loop.state.running is False


async def test_screen_awareness_error_carries_reason() -> None:
    error = ScreenAwarenessError("screen_locked")
    assert error.reason_code == "screen_locked"


async def test_config_requires_displays_when_enabled() -> None:
    with pytest.raises(Exception, match="at least one display"):
        make_config(displays=[])


async def test_unchanged_threshold_zero_still_compares(migrated: Database) -> None:
    gateway = FakeGateway(make_png((120, 120, 120)))
    analyzer = FakeAnalyzer(ANALYSIS_JSON)
    config = make_config(unchanged_skip_threshold=0)
    loop, _owner = make_loop(migrated, config, gateway, analyzer)
    await loop._tick(config.screen_awareness)
    assert len(analyzer.calls) == 2
    # 阈值 0：完全相同的哈希也跳过
    await loop._tick(config.screen_awareness)
    assert len(analyzer.calls) == 2


async def test_expires_proactive_event_window(migrated: Database) -> None:
    perception = FakePerception()
    loop, _owner = make_loop(
        migrated,
        make_config(),
        FakeGateway(make_png((1, 2, 3))),
        FakeAnalyzer(ANALYSIS_JSON),
        perception=perception,
    )
    await loop._tick(make_config().screen_awareness)
    event = perception.submitted[0][0]
    assert event.expires_at is not None
    assert event.expires_at > datetime.now(UTC)
