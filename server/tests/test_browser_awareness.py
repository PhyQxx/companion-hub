from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import create_admin_browser_awareness_router
from app.browser_awareness import (
    BrowserAnalysis,
    BrowserAwarenessError,
    BrowserAwarenessLoop,
    parse_analysis,
)
from app.config.models import BrowserAwarenessConfig
from app.db import Base, Database, create_database
from app.ids import uuid7
from app.memory.models import MemoryType
from app.timeline.models import TimelineSourceType
from app.timeline.store import TimelineStore


def document(*, origin: str = "https://example.com/private/path?token=secret") -> bytes:
    return json.dumps(
        {
            "title": "项目计划",
            "origin": origin,
            "language": "zh-CN",
            "text": "正文中的计划内容",
            "truncated": False,
        }
    ).encode()


@dataclass
class FakeCommand:
    id: UUID = field(default_factory=uuid7)
    status: str = "succeeded"
    reason_code: str | None = None
    result_meta: dict[str, Any] | None = field(
        default_factory=lambda: {"asset_id": str(uuid7())}
    )


@dataclass
class FakeAsset:
    data: bytes


class FakeAssets:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.consumed = 0

    async def consume(self, *_: Any, **__: Any) -> FakeAsset:
        self.consumed += 1
        return FakeAsset(self.data)


class FakeGateway:
    def __init__(self, data: bytes, *, fail: bool = False) -> None:
        self.assets = FakeAssets(data)
        self.fail = fail
        self.issued: list[dict[str, Any]] = []

    async def issue(self, **kwargs: Any) -> FakeCommand:
        self.issued.append(kwargs)
        if self.fail:
            return FakeCommand(status="failed", reason_code="browser_unavailable")
        return FakeCommand()

    async def wait_for_terminal(self, *_: Any, **__: Any) -> FakeCommand:
        raise AssertionError("terminal command must not wait")


class FakeResolver:
    def __init__(self) -> None:
        self.device = type("Device", (), {"id": uuid7()})()

    async def resolve(self, **_: Any) -> Any:
        return self.device


class FakeAnalyzer:
    def __init__(self, analysis: BrowserAnalysis | None = None) -> None:
        self.analysis = analysis or BrowserAnalysis("用户正在查看项目计划", True, True, "要提醒吗")
        self.calls: list[dict[str, Any]] = []

    async def analyze(self, **kwargs: Any) -> BrowserAnalysis:
        self.calls.append(kwargs)
        return self.analysis


class FakeMemory:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def ingest(self, candidate: Any, **kwargs: Any) -> Any:
        self.items.append((candidate, kwargs))
        return None


class FakePerception:
    def __init__(self) -> None:
        self.items: list[tuple[Any, float]] = []

    def submit(self, event: Any, *, stable_for_seconds: float, **_: Any) -> None:
        self.items.append((event, stable_for_seconds))


class FakeTabHints:
    def __init__(self, origin: str | None = None, title: str | None = None) -> None:
        self.hint = type("Hint", (), {"origin": origin, "title": title})() if origin else None
        self.enabled_calls: list[bool] = []

    def set_tab_hint_enabled(self, enabled: bool) -> None:
        self.enabled_calls.append(enabled)

    def tab_hint_for(self, device_id: UUID) -> Any:
        return self.hint


@pytest.fixture
async def database() -> Database:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return value


def make_loop(
    database: Database,
    gateway: FakeGateway,
    analyzer: FakeAnalyzer,
    *,
    memory: FakeMemory | None = None,
    perception: FakePerception | None = None,
    tab_hints: FakeTabHints | None = None,
) -> tuple[BrowserAwarenessLoop, UUID]:
    owner = uuid7()
    config = BrowserAwarenessConfig(enabled=True, interval_seconds=15)
    config_value = type("Config", (), {"browser_awareness": config})()
    snapshot = type("Snapshot", (), {"config": config_value})()
    config_store = type("ConfigStore", (), {"current": snapshot})()
    loop = BrowserAwarenessLoop(
        config_store=config_store,
        database=database,
        resolver=FakeResolver(),
        gateway=cast(Any, gateway),
        analyzer=analyzer,
        timeline=TimelineStore(database),
        memory_ingester=cast(Any, memory),
        perception_pipeline=perception,
        tab_hints=cast(Any, tab_hints),
        clock=lambda: datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
    )

    async def resolve_owner() -> UUID:
        return owner

    loop._resolve_owner = resolve_owner  # type: ignore[method-assign]
    return loop, owner


async def test_disabled_browser_awareness_sends_no_command(database: Database) -> None:
    gateway = FakeGateway(document())
    analyzer = FakeAnalyzer()
    loop, _ = make_loop(database, gateway, analyzer)

    await loop._tick(BrowserAwarenessConfig(enabled=False))

    assert gateway.issued == []
    assert analyzer.calls == []


async def test_browser_tick_persists_only_origin_summary_and_branches(
    database: Database,
) -> None:
    gateway = FakeGateway(document())
    analyzer = FakeAnalyzer()
    memory = FakeMemory()
    perception = FakePerception()
    loop, owner = make_loop(
        database, gateway, analyzer, memory=memory, perception=perception
    )

    await loop._tick(BrowserAwarenessConfig(enabled=True, interval_seconds=15))

    result = await TimelineStore(database).search(
        user_id=owner,
        source_types=(TimelineSourceType.DEVICE,),
        event_types=("browser.observed",),
    )
    assert len(result.events) == 1
    event = result.events[0]
    assert event.metadata["origin"] == "https://example.com"
    assert "private" not in json.dumps(event.metadata)
    assert "正文" not in json.dumps(event.metadata)
    assert memory.items[0][0].type == MemoryType.EPISODIC
    assert memory.items[0][0].extractor_version == "browser-v1"
    assert perception.items[0][0].kind == "browser.observed"
    assert perception.items[0][1] == 10


async def test_same_origin_and_title_skips_second_analysis(database: Database) -> None:
    gateway = FakeGateway(document())
    analyzer = FakeAnalyzer()
    loop, _ = make_loop(database, gateway, analyzer)
    config = BrowserAwarenessConfig(enabled=True, interval_seconds=15)

    await loop._tick(config)
    await loop._tick(config)

    assert len(gateway.issued) == 2
    assert len(analyzer.calls) == 1
    assert loop.state.observations == 1


async def test_blocked_and_non_http_origins_are_silently_skipped(database: Database) -> None:
    analyzer = FakeAnalyzer()
    blocked, _ = make_loop(database, FakeGateway(document()), analyzer)
    await blocked._tick(
        BrowserAwarenessConfig(
            enabled=True,
            interval_seconds=15,
            blocked_hosts=["EXAMPLE.COM"],
        )
    )
    local, _ = make_loop(database, FakeGateway(document(origin="chrome://settings")), analyzer)
    await local._tick(BrowserAwarenessConfig(enabled=True, interval_seconds=15))

    assert analyzer.calls == []
    assert blocked.state.last_error is None
    assert local.state.last_error is None


async def test_three_failures_enter_cooldown(database: Database) -> None:
    gateway = FakeGateway(document(), fail=True)
    loop, _ = make_loop(database, gateway, FakeAnalyzer())
    config = BrowserAwarenessConfig(enabled=True, interval_seconds=15)

    await loop._tick(config)
    await loop._tick(config)
    await loop._tick(config)
    await loop._tick(config)

    assert len(gateway.issued) == 3
    assert loop.state.cooldown_until is not None
    assert loop.state.last_error == "browser_unavailable"


def test_browser_analysis_bad_json_falls_back_without_escalation() -> None:
    result = parse_analysis("普通网页摘要")
    assert result.summary == "普通网页摘要"
    assert result.notable is False
    assert result.memory_worthy is False


async def test_tab_hint_unchanged_skips_read_command(database: Database) -> None:
    gateway = FakeGateway(document())
    analyzer = FakeAnalyzer()
    hints = FakeTabHints()
    loop, _ = make_loop(database, gateway, analyzer, tab_hints=hints)
    config = BrowserAwarenessConfig(enabled=True, interval_seconds=15)

    await loop._tick(config)
    assert len(gateway.issued) == 1
    assert hints.enabled_calls == [True]

    # 心跳指纹与上次观察一致：不再下发 read，也不调用模型
    hints.hint = type("Hint", (), {"origin": "https://example.com", "title": "项目计划"})()
    await loop._tick(config)
    assert len(gateway.issued) == 1
    assert len(analyzer.calls) == 1
    assert loop.state.observations == 1

    # 指纹变化（标题不同）则恢复读取
    hints.hint = type("Hint", (), {"origin": "https://example.com", "title": "新页面"})()
    await loop._tick(config)
    assert len(gateway.issued) == 2


async def test_disabled_tick_stops_hint_reporting(database: Database) -> None:
    hints = FakeTabHints()
    loop, _ = make_loop(database, FakeGateway(document()), FakeAnalyzer(), tab_hints=hints)

    await loop._tick(BrowserAwarenessConfig(enabled=False))

    assert hints.enabled_calls == [False]


def test_browser_awareness_config_bounds_and_normalizes_hosts() -> None:
    config = BrowserAwarenessConfig(blocked_hosts=["Bank.Example"])
    assert config.blocked_hosts == ["bank.example"]
    with pytest.raises(ValueError, match="host names"):
        BrowserAwarenessConfig(blocked_hosts=["https://bank.example/login"])
    with pytest.raises(ValueError):
        BrowserAwarenessConfig(interval_seconds=14)


def test_browser_awareness_error_carries_reason() -> None:
    assert BrowserAwarenessError("bad_document").reason_code == "bad_document"


async def test_browser_awareness_admin_endpoints_degrade_without_loop(
    database: Database,
) -> None:
    app = FastAPI()
    app.include_router(
        create_admin_browser_awareness_router(
            TimelineStore(database), admin_token="admin-secret"
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/api/v1/admin/browser-awareness/status")
        status = await client.get(
            "/api/v1/admin/browser-awareness/status",
            headers={"Authorization": "Bearer admin-secret"},
        )
        observations = await client.get(
            "/api/v1/admin/browser-awareness/observations",
            headers={"Authorization": "Bearer admin-secret"},
        )

    assert unauthorized.status_code == 401
    assert status.status_code == 200
    assert status.json()["loop_running"] is False
    assert observations.json() == {"items": [], "total": 0}
