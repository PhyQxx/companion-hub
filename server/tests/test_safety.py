from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

from app.config.models import SafetyConfig
from app.db import AppUserRecord, Base, Database, create_database
from app.home_assistant import HomeAssistantProactiveEngine
from app.home_assistant.models import HomeAssistantState
from app.ids import uuid7
from app.safety import SafetyAlertService
from app.timeline.models import TimelineSourceType
from app.timeline.store import TimelineStore


class FakeTime:
    """可控时钟：sleep 直接推进时间，窗口判定完全确定性。"""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

    def clock(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def _database(tmp_path: Any) -> Database:
    # 状态机后台任务与测试协程并发开连接，:memory: 每连接独立库会互相看不到表，
    # 用文件库保证并发会话共享 schema
    database = create_database(f"sqlite+aiosqlite:///{tmp_path}/safety.db")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return database


def _config_store(*, window: int = 60, retry: int = 3) -> Any:
    safety = SafetyConfig(enabled=True, confirm_window_seconds=window, push_retry_minutes=retry)
    return SimpleNamespace(
        current=SimpleNamespace(config=SimpleNamespace(safety=safety))
    )


def _make_service(
    database: Database,
    fake_time: FakeTime,
    config_store: Any | None = None,
    timeline: TimelineStore | None = None,
) -> tuple[SafetyAlertService, list[dict[str, Any]]]:
    deliveries: list[dict[str, Any]] = []

    async def deliver(text: str, **kwargs: Any) -> Any:
        deliveries.append({"text": text, **kwargs})
        return SimpleNamespace(user_id=kwargs.get("target_user_id"), conversation_id=None)

    service = SafetyAlertService(
        database,
        config_store or _config_store(),
        cast(Any, deliver),
        timeline=timeline,
        clock=fake_time.clock,
        sleeper=fake_time.sleep,
    )
    return service, deliveries


async def test_alert_lifecycle_l1_l2_repeat_ack(tmp_path: Any) -> None:
    database = await _database(tmp_path)
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user_id, display_name="Owner", status="active"))
    timeline = TimelineStore(database)
    fake_time = FakeTime()
    service, deliveries = _make_service(database, fake_time, timeline=timeline)
    try:
        alert = await service.handle(
            user_id=user_id,
            rule_id="smoke",
            entity_id="binary_sensor.smoke",
            message="【危急】厨房烟感触发了烟雾告警…",
            evidence={"entity": "binary_sensor.smoke", "confidence": 0.6},
        )
        assert alert is not None and alert.status == "escalating" and alert.level == 1
        assert len(deliveries) == 1
        assert deliveries[0]["broadcast"] is True
        assert deliveries[0]["trigger_kind"] == "safety.alert"

        # 确认窗口（60s）过后升级 L2，再等重试间隔（3min）重复提醒，
        # 最后因无预授权联系人停留在 L2 并提示无法升级（S3 行为）
        await service._tasks[alert.id]
        assert len(deliveries) == 4
        assert deliveries[1]["text"].startswith("【仍需确认】")
        assert deliveries[2]["text"].startswith("【再次提醒】")
        assert "未配置预授权紧急联系人" in deliveries[3]["text"]
        fresh = await service._store.get(alert.id)
        assert fresh is not None and fresh.level == 2 and fresh.l2_at is not None

        # 到期收尾为 expired（升级链在首次 await 时已跑完，任务自动出表）
        expired = await service._store.get(alert.id)
        assert expired is not None and expired.status == "expired"

        events = await timeline.search(
            user_id=user_id,
            source_types=(TimelineSourceType.SYSTEM,),
            event_types=("safety.alert_raised", "safety.alert_escalated"),
        )
        kinds = [event.event_type for event in events.events]
        assert "safety.alert_raised" in kinds and "safety.alert_escalated" in kinds
    finally:
        await service.stop()
        await database.close()


async def test_ack_terminates_escalation_and_chat_intent_matches(tmp_path: Any) -> None:
    database = await _database(tmp_path)
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user_id, display_name="Owner", status="active"))
    fake_time = FakeTime()
    service, deliveries = _make_service(database, fake_time)
    try:
        alert = await service.handle(
            user_id=user_id,
            rule_id="leak",
            entity_id="binary_sensor.leak",
            message="【危急】厨房水浸…",
            evidence={},
        )
        assert alert is not None

        assert await service.handle_user_text("今天天气怎么样", user_id=user_id) == 0
        assert await service.handle_user_text("知道了。", user_id=user_id) == 1
        acked = await service._store.get(alert.id)
        assert acked is not None and acked.status == "acknowledged"
        assert acked.ack_source == "chat"

        # 确认后不再升级投递
        await asyncio.sleep(0)
        assert len(deliveries) == 1
    finally:
        await service.stop()
        await database.close()


async def test_handle_dedupes_active_alert(tmp_path: Any) -> None:
    database = await _database(tmp_path)
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user_id, display_name="Owner", status="active"))
    fake_time = FakeTime()
    service, deliveries = _make_service(database, fake_time)
    try:
        first = await service.handle(
            user_id=user_id, rule_id="smoke", entity_id="binary_sensor.smoke",
            message="m", evidence={},
        )
        second = await service.handle(
            user_id=user_id, rule_id="smoke", entity_id="binary_sensor.smoke",
            message="m", evidence={},
        )
        assert first is not None and second is not None and first.id == second.id
        assert len(deliveries) == 1
    finally:
        await service.stop()
        await database.close()


async def test_resume_picks_up_escalating_alerts_after_restart(tmp_path: Any) -> None:
    database = await _database(tmp_path)
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user_id, display_name="Owner", status="active"))
    fake_time = FakeTime()
    first, _ = _make_service(database, fake_time)
    alert = await first.handle(
        user_id=user_id, rule_id="smoke", entity_id="binary_sensor.smoke",
        message="m", evidence={},
    )
    await first.stop()
    assert alert is not None

    # “重启”：新服务实例 + 时间已过确认窗口 → resume 后直接补 L2
    fake_time.now += timedelta(minutes=10)
    second, deliveries2 = _make_service(database, fake_time)
    try:
        resumed = await second.resume()
        assert resumed == 1
        task = second._tasks.get(alert.id)
        if task is not None:
            await task
        assert any(d["text"].startswith("【仍需确认】") for d in deliveries2)
    finally:
        await second.stop()
        await database.close()


async def test_engine_routes_critical_to_state_machine(tmp_path: Any) -> None:
    from app.config import (
        HomeAssistantConfig,
        HomeAssistantEntityConfig,
    )

    database = await _database(tmp_path)
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user_id, display_name="Owner", status="active"))
    fake_time = FakeTime()
    safety, safety_deliveries = _make_service(database, fake_time)

    engine_deliveries: list[dict[str, Any]] = []

    async def engine_deliver(text: str, **kwargs: Any) -> Any:
        engine_deliveries.append({"text": text, **kwargs})
        return SimpleNamespace(user_id=user_id, conversation_id=None)

    def _entity(entity_id: str, name: str) -> HomeAssistantEntityConfig:
        return HomeAssistantEntityConfig.model_validate(
            {
                "entity_id": entity_id,
                "display_name": name,
                "aliases": [],
                "read_allowed": True,
                "history_allowed": False,
                "history_max_hours": 24,
                "allowed_actions": [],
                "confirmation_required_actions": [],
                "privacy_level": "L1",
                "allowed_attributes": ["friendly_name"],
                "proactive_rules": [
                    {
                        "rule_id": "smoke",
                        "kind": "smoke_detected",
                        "enabled": True,
                        "severity": "critical",
                        "duration_seconds": 0,
                        "cooldown_minutes": 30,
                    }
                ],
            }
        )

    smoke_state = HomeAssistantState(
        entity_id="binary_sensor.smoke", state="on", attributes={},
        last_changed=None, last_updated=None,
    )
    store = SimpleNamespace(
        current=SimpleNamespace(
            config=SimpleNamespace(
                integrations=SimpleNamespace(
                    home_assistant=HomeAssistantConfig.model_validate(
                        {
                            "enabled": True,
                            "base_url": "https://ha.example.test:8123",
                            "secret_ref": "env:ARIA_HA_TOKEN",
                            "entities": [_entity("binary_sensor.smoke", "厨房烟感").model_dump()],
                        }
                    )
                ),
                safety=SafetyConfig(enabled=True),
            )
        )
    )
    policy = store.current.config.integrations.home_assistant.entities[0]
    rule = policy.proactive_rules[0]
    engine = HomeAssistantProactiveEngine(
        database,
        cast(Any, store),
        lambda entity_id: smoke_state,
        cast(Any, engine_deliver),
        safety=safety,
    )
    try:
        await engine._fire(policy, rule, smoke_state)
        # 引擎不再直投，告警进入状态机（L1 由状态机广播）
        assert engine_deliveries == []
        assert len(safety_deliveries) == 1
        assert safety_deliveries[0]["text"].startswith("【危急】")
        assert safety_deliveries[0]["broadcast"] is True
    finally:
        await engine.stop()
        await safety.stop()
        await database.close()


class FakeMailer:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict[str, Any]] = []

    async def send(self, *, to: list[str], subject: str, body: str, **_: Any) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("smtp down")
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"message_id": "mid-1", "recipients": to}


def _make_service_with_mailer(
    database: Database, fake_time: FakeTime, mailer: FakeMailer | None
) -> tuple[SafetyAlertService, list[dict[str, Any]]]:
    deliveries: list[dict[str, Any]] = []

    async def deliver(text: str, **kwargs: Any) -> Any:
        deliveries.append({"text": text, **kwargs})
        return SimpleNamespace(user_id=kwargs.get("target_user_id"), conversation_id=None)

    service = SafetyAlertService(
        database,
        _config_store(),
        cast(Any, deliver),
        timeline=TimelineStore(database),
        mailer=mailer,
        clock=fake_time.clock,
        sleeper=fake_time.sleep,
    )
    return service, deliveries


async def test_l3_escalation_sends_email_with_pre_notice_and_ledger(tmp_path: Any) -> None:
    database = await _database(tmp_path)
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user_id, display_name="Owner", status="active"))
    fake_time = FakeTime()
    mailer = FakeMailer()
    service, deliveries = _make_service_with_mailer(database, fake_time, mailer)
    try:
        authorization = await service.authorizations.create(
            user_id=user_id, contact_name="张三", destination="zhang@example.com",
        )
        assert authorization.status == "active"

        alert = await service.handle(
            user_id=user_id, rule_id="smoke", entity_id="binary_sensor.smoke",
            message="【危急】烟雾告警…", evidence={},
        )
        assert alert is not None
        task = service._tasks.get(alert.id)
        if task is not None:
            await task

        # L1 → L2 → 重提醒 → 发送前告知 → 邮件
        pre_notice = next(
            (d for d in deliveries if "正在通过邮件通知紧急联系人" in str(d["text"])), None
        )
        assert pre_notice is not None and pre_notice["broadcast"] is True
        assert len(mailer.sent) == 1
        assert mailer.sent[0]["to"] == ["zhang@example.com"]
        assert "烟雾告警" in mailer.sent[0]["body"]

        # 台账 + Timeline 记录 L3
        assert await service.ledger.contacted(alert.id) is True
        timeline = TimelineStore(database)
        events = await timeline.search(
            user_id=user_id, event_types=("safety.alert_escalated",),
        )
        escalated = [e for e in events.events if (e.metadata or {}).get("level") == 3]
        assert escalated and escalated[0].metadata["channel"] == "email"
    finally:
        await service.stop()
        await database.close()


async def test_l3_without_authorization_stays_l2_and_notifies_user(tmp_path: Any) -> None:
    database = await _database(tmp_path)
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user_id, display_name="Owner", status="active"))
    fake_time = FakeTime()
    mailer = FakeMailer()
    service, deliveries = _make_service_with_mailer(database, fake_time, mailer)
    try:
        alert = await service.handle(
            user_id=user_id, rule_id="smoke", entity_id="binary_sensor.smoke",
            message="【危急】烟雾告警…", evidence={},
        )
        assert alert is not None
        task = service._tasks.get(alert.id)
        if task is not None:
            await task

        assert mailer.sent == []
        assert await service.ledger.contacted(alert.id) is False
        notice = next(
            (d for d in deliveries if "未配置预授权紧急联系人" in str(d["text"])), None
        )
        assert notice is not None
    finally:
        await service.stop()
        await database.close()


async def test_l3_mail_failure_records_ledger_and_revoke(tmp_path: Any) -> None:
    database = await _database(tmp_path)
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user_id, display_name="Owner", status="active"))
    fake_time = FakeTime()
    mailer = FakeMailer(fail=True)
    service, _ = _make_service_with_mailer(database, fake_time, mailer)
    try:
        await service.authorizations.create(
            user_id=user_id, contact_name="李四", destination="li@example.com",
        )
        alert = await service.handle(
            user_id=user_id, rule_id="smoke", entity_id="binary_sensor.smoke",
            message="【危急】烟雾告警…", evidence={},
        )
        assert alert is not None
        task = service._tasks.get(alert.id)
        if task is not None:
            await task

        from sqlalchemy import select as sa_select

        from app.db import SafetyAlertEscalationRecord
        async with database.sessions() as session:
            rows: list[SafetyAlertEscalationRecord] = list(
                await session.scalars(sa_select(SafetyAlertEscalationRecord))
            )
        assert len(rows) == 1 and rows[0].status == "failed"

        revoked = await service.authorizations.revoke(
            (await service.authorizations.list_for_user(user_id))[0].id,
            at=fake_time.clock(),
        )
        assert revoked is not None and revoked.status == "revoked"
        assert await service.authorizations.active_for_user(user_id) is None
    finally:
        await service.stop()
        await database.close()


async def test_activity_inactivity_reminds_within_window_and_respects_cooldown(
    tmp_path: Any,
) -> None:
    from app.safety import ActivityTracker, SafetyActivityScheduler

    database = await _database(tmp_path)
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(
            id=user_id, display_name="Owner", status="active", timezone="Asia/Shanghai",
        ))

    class FakeTime2:
        def __init__(self) -> None:
            self.now = datetime(2026, 9, 13, 13, 0, tzinfo=UTC)  # 北京 21:00，窗口内
        def clock(self) -> datetime:
            return self.now
        async def sleep(self, seconds: float) -> None:
            self.now += timedelta(seconds=seconds)

    fake_time = FakeTime2()
    deliveries: list[dict[str, Any]] = []

    async def deliver(text: str, **kwargs: Any) -> Any:
        deliveries.append({"text": text, **kwargs})
        return SimpleNamespace(user_id=user_id, conversation_id=None)

    config = SimpleNamespace(current=SimpleNamespace(config=SimpleNamespace(
        safety=SafetyConfig(enabled=True, inactivity_hours=12.0),
    )))
    tracker = ActivityTracker()
    # 最后活动 = 24h 前（超阈值）
    tracker.record(user_id, fake_time.now - timedelta(hours=24))
    scheduler = SafetyActivityScheduler(
        database, config, cast(Any, deliver), tracker,
        clock=fake_time.clock, sleeper=fake_time.sleep,
    )
    safety_config = config.current.config.safety
    try:
        await scheduler._tick(safety_config)
        assert len(deliveries) == 1
        assert "没有你的任何活动记录" in deliveries[0]["text"]
        assert deliveries[0]["trigger_kind"] == "user.inactive"

        # 冷却期内不重复打扰
        await scheduler._tick(safety_config)
        assert len(deliveries) == 1

        # 活动刷新后重新计时：阈值未到不提醒
        tracker.record(user_id, fake_time.now - timedelta(hours=1))
        scheduler._last_reminded.clear()
        await scheduler._tick(safety_config)
        assert len(deliveries) == 1
    finally:
        await scheduler.stop()
        await database.close()


async def test_activity_silent_outside_window_or_when_device_recent(tmp_path: Any) -> None:
    from app.safety import ActivityTracker, SafetyActivityScheduler

    database = await _database(tmp_path)
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(
            id=user_id, display_name="Owner", status="active", timezone="Asia/Shanghai",
        ))
        # 桌面设备 1 小时前有心跳 → 设备信号刷新活动
        from app.db import DeviceClientRecord
        session.add(DeviceClientRecord(
            id=uuid7(), owner_user_id=user_id, name="Mac", alias=None,
            client_type="desktop", credential_hash="x" * 64,
            capabilities=[], granted_capabilities=[],
            paired_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
            last_seen_at=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),  # 北京 20:00，1h 前心跳
        ))

    class FakeTime3:
        def __init__(self) -> None:
            self.now = datetime(2026, 9, 13, 13, 0, tzinfo=UTC)  # 北京 21:00，窗口内
        def clock(self) -> datetime:
            return self.now
        async def sleep(self, seconds: float) -> None:
            self.now += timedelta(seconds=seconds)

    fake_time = FakeTime3()
    deliveries: list[dict[str, Any]] = []

    async def deliver(text: str, **kwargs: Any) -> Any:
        deliveries.append({"text": text, **kwargs})
        return SimpleNamespace(user_id=user_id, conversation_id=None)

    config = SimpleNamespace(current=SimpleNamespace(config=SimpleNamespace(
        safety=SafetyConfig(enabled=True, inactivity_hours=12.0),
    )))
    tracker = ActivityTracker()
    tracker.record(user_id, fake_time.now - timedelta(hours=30))
    scheduler = SafetyActivityScheduler(
        database, config, cast(Any, deliver), tracker,
        clock=fake_time.clock, sleeper=fake_time.sleep,
    )
    safety_config = config.current.config.safety
    try:
        # 设备心跳（1h 前）比 tracker 记录新：以设备心跳为准，不提醒
        await scheduler._tick(safety_config)
        assert deliveries == []

        # 生效时段外（北京 04:00）不提醒
        tracker.record(user_id, fake_time.now - timedelta(hours=30))
        fake_time.now = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)  # 北京 04:00
        await scheduler._tick(safety_config)
        assert deliveries == []

        # 回到窗口内且时间已过心跳 24h+：恢复提醒
        fake_time.now = datetime(2026, 9, 14, 5, 0, tzinfo=UTC)  # 北京 13:00，窗口内
        await scheduler._tick(safety_config)
        assert len(deliveries) == 1
    finally:
        await scheduler.stop()
        await database.close()


async def test_chat_safety_api_lists_and_acks_alerts(tmp_path: Any) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api import create_safety_router
    from app.auth import AuthService

    database = await _database(tmp_path)
    password = "correct horse battery staple"
    auth = AuthService(database)
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(
            id=uuid7(), display_name="Owner", status="active", timezone="Asia/Shanghai",
        ))
    await auth.setup(display_name="Owner", password=password)
    login = await auth.login(password=password)

    fake_time = FakeTime()
    service, _ = _make_service(database, fake_time)
    alert = await service.handle(
        user_id=login.principal.user_id, rule_id="smoke", entity_id="binary_sensor.smoke",
        message="【危急】厨房烟感触发了烟雾告警…", evidence={},
    )
    assert alert is not None
    # FakeTime 的 sleep 即时推进：后台升级链会在首个 await 处瞬间走完生命周期并把
    # 告警终态化，与 HTTP ack 竞态（偶发 409/锁冲突）。先停升级任务再断言 ack 语义。
    await service.stop()

    app = FastAPI()
    app.include_router(create_safety_router(service, auth))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/api/v1/safety/alerts")
        listed = await client.get(
            "/api/v1/safety/alerts",
            headers={"Authorization": f"Bearer {login.access_token}"},
        )
        acked = await client.post(
            f"/api/v1/safety/alerts/{alert.id}/ack",
            headers={"Authorization": f"Bearer {login.access_token}"},
        )
        replay = await client.post(
            f"/api/v1/safety/alerts/{alert.id}/ack",
            headers={"Authorization": f"Bearer {login.access_token}"},
        )
        after = await client.get(
            "/api/v1/safety/alerts",
            headers={"Authorization": f"Bearer {login.access_token}"},
        )

    assert unauthorized.status_code in {401, 422}
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1 and items[0]["message"].startswith("【危急】")
    assert acked.status_code == 200 and acked.json()["status"] == "acknowledged"
    # 已确认的重复 ack 幂等返回 200 + acknowledged 状态
    assert replay.status_code == 200 and replay.json()["status"] == "acknowledged"
    assert after.json()["total"] == 0
    await service.stop()
    await database.close()
