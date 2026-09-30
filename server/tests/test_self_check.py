"""RPT 每日自体检（docs/09 §7）：确定性检查、异常才说话、每日至多一次。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from app.config import ConfigStore
from app.db import AppUserRecord, Base, Database, create_database
from app.devices.service import DeviceRegistry
from app.llm import CompletionRequest, CompletionResult, LLMRoute
from app.observability.selfcheck import DailySelfCheckScheduler, SelfCheckFinding


@dataclass
class FakeDevice:
    name: str
    last_seen_at: datetime
    revoked_at: object | None = None


@dataclass
class FakeRegistry:
    devices: list[object] = field(default_factory=list)

    async def list_devices(self) -> list[object]:
        return list(self.devices)


class FlakyBackend:
    """可配置成功/失败的探活后端。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("gateway down")
        return CompletionResult(
            text="pong",
            provider="fake",
            model="fake",
            endpoint="fake",
            route=LLMRoute.UTILITY,
            latency_ms=1,
            finish_reason="stop",
        )


class RecordingDeliverer:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.user_ids: list[UUID] = []

    async def __call__(
        self,
        text: str,
        *,
        entity_id: str,
        rule_id: str,
        trigger_kind: str,
        privacy_level: object,
        target_user_id: UUID,
    ) -> None:
        self.texts.append(text)
        self.user_ids.append(target_user_id)


def _scheduler(
    database: Database,
    deliverer: RecordingDeliverer,
    *,
    registry: FakeRegistry | None = None,
    backend: FlakyBackend | None = None,
    backup_dir: Path | None = None,
    now: datetime | None = None,
    isolate: Path | None = None,
) -> DailySelfCheckScheduler:
    return DailySelfCheckScheduler(
        database,
        config_store=None,
        device_registry=cast(DeviceRegistry, registry),
        # 未显式给目录时指向不存在的路径，隔离本机 backups/ 的真实状态
        backup_dir=backup_dir if backup_dir is not None else (isolate or Path("/nonexistent")),
        clock=lambda: now or datetime(2026, 10, 1, 10, 30, tzinfo=UTC),
        deliverer=deliverer,
        router_builder=(lambda config: backend) if backend is not None else None,
    )


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with value.sessions() as session:
        session.add(AppUserRecord(id=uuid4(), display_name="主人", status="active"))
        await session.commit()
    yield value


async def _second_user_id(database: Database) -> UUID:
    async with database.sessions() as session:
        from sqlalchemy import insert

        result = await session.execute(
            insert(AppUserRecord)
            .values(id=uuid4(), display_name="第二用户", status="active")
            .returning(AppUserRecord.id)
        )
        await session.commit()
        return result.scalar_one()


@pytest.mark.asyncio
async def test_time_gate_and_daily_once(database: Database, tmp_path: Path) -> None:
    deliverer = RecordingDeliverer()
    scheduler = _scheduler(database, deliverer, backup_dir=tmp_path / "none")
    # 早于检查时刻（默认 10:00）：不体检
    early = await scheduler.run_once(now=datetime(2026, 10, 1, 8, 0, tzinfo=UTC))
    assert early == [] and deliverer.texts == []
    # 到点体检：健康 → 静默
    healthy = await scheduler.run_once(now=datetime(2026, 10, 1, 10, 30, tzinfo=UTC))
    assert healthy == [] and deliverer.texts == []
    # 同日再次到点：不重复体检
    again = await scheduler.run_once(now=datetime(2026, 10, 1, 18, 0, tzinfo=UTC))
    assert again == []


@pytest.mark.asyncio
async def test_backup_staleness_reported_once_per_day(
    database: Database, tmp_path: Path
) -> None:
    dump = tmp_path / "aria_20261001.dump"
    dump.write_bytes(b"pg_dump")
    stale_moment = datetime(2026, 10, 1, 10, 30, tzinfo=UTC) - timedelta(hours=30)
    os.utime(dump, (stale_moment.timestamp(), stale_moment.timestamp()))
    deliverer = RecordingDeliverer()
    scheduler = _scheduler(database, deliverer, backup_dir=tmp_path)
    findings = await scheduler.run_once()
    assert len(findings) == 1
    assert findings[0].check == "备份落盘"
    assert "26 小时" in findings[0].detail
    assert deliverer.texts and "1 项异常" in deliverer.texts[0]
    # 同日不再重复投递
    assert await scheduler.run_once() == []
    assert len(deliverer.texts) == 1


@pytest.mark.asyncio
async def test_backup_fresh_or_missing_is_silent(
    database: Database, tmp_path: Path
) -> None:
    deliverer = RecordingDeliverer()
    # 新鲜备份：安静
    fresh = tmp_path / "aria_fresh.dump"
    fresh.write_bytes(b"pg_dump")
    os.utime(fresh, (datetime(2026, 10, 1, 9, 0, tzinfo=UTC).timestamp(),) * 2)
    scheduler = _scheduler(database, deliverer, backup_dir=tmp_path)
    assert await scheduler.run_once() == []
    # 目录缺失（未启用备份）：安静不制造噪音
    missing = _scheduler(database, deliverer, backup_dir=tmp_path / "nope")
    assert await missing.run_once() == []
    assert deliverer.texts == []


@pytest.mark.asyncio
async def test_device_heartbeat_window(database: Database, tmp_path: Path) -> None:
    now = datetime(2026, 10, 1, 10, 30, tzinfo=UTC)
    registry = FakeRegistry(
        devices=[
            FakeDevice(name="客厅 Mac", last_seen_at=now - timedelta(hours=48)),
            FakeDevice(name="昨天的手机", last_seen_at=now - timedelta(hours=2)),
            FakeDevice(
                name="已注销", last_seen_at=now - timedelta(hours=48), revoked_at=now
            ),
            # 停用太久（>7 天）：不再提醒
            FakeDevice(name="退役设备", last_seen_at=now - timedelta(days=30)),
        ]
    )
    deliverer = RecordingDeliverer()
    scheduler = _scheduler(
        database, deliverer, registry=registry, now=now, isolate=tmp_path
    )
    findings = await scheduler.run_once()
    assert [item.check for item in findings] == ["设备心跳"]
    assert "客厅 Mac" in findings[0].detail
    assert deliverer.texts and "1 项异常" in deliverer.texts[0]


@pytest.mark.asyncio
async def test_route_probe_failure_and_delivery_to_active_users(
    database: Database, tmp_path: Path
) -> None:
    second = await _second_user_id(database)
    deliverer = RecordingDeliverer()
    failing = FlakyBackend(fail=True)
    scheduler = _scheduler(
        database,
        deliverer,
        backend=failing,
        now=datetime(2026, 10, 1, 10, 30, tzinfo=UTC),
        isolate=tmp_path,
    )
    # 直接注入配置快照路径：config_store=None 会跳过路由检查，这里换成 Fake
    scheduler._config_store = cast(ConfigStore, _StaticConfig())
    findings = await scheduler.run_once()
    assert [item.check for item in findings] == ["模型路由"]
    assert failing.calls == 1
    # 投递给全部活跃用户（每人一条）
    assert len(deliverer.texts) == 2
    assert len(deliverer.user_ids) == 2
    assert second in deliverer.user_ids


class _StaticConfig:
    """最小配置桩：自体检只读 snapshot.config 交给注入的 router_builder。"""

    current = type("Snapshot", (), {"config": object()})()


@pytest.mark.asyncio
async def test_route_probe_success_is_silent(database: Database, tmp_path: Path) -> None:
    deliverer = RecordingDeliverer()
    scheduler = _scheduler(
        database,
        deliverer,
        backend=FlakyBackend(),
        now=datetime(2026, 10, 1, 10, 30, tzinfo=UTC),
        isolate=tmp_path,
    )
    scheduler._config_store = cast(ConfigStore, _StaticConfig())
    assert await scheduler.run_once() == []
    assert deliverer.texts == []


@pytest.mark.asyncio
async def test_finding_render_is_deterministic() -> None:
    finding = SelfCheckFinding(check="模型路由", detail="探活失败")
    assert finding.render() == "- 模型路由：探活失败"
