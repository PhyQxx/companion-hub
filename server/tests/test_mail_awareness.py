"""MAILW（docs/09 §3）邮件感知循环：基线、增量分析、认知裁决与冷却。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import yaml

from app.cognition.models import SemanticEvent
from app.config import HubConfig
from app.config.models import MailAwarenessConfig
from app.mail.client import MailError, MailSummary
from app.mail_awareness import MailAnalysis, MailAwarenessLoop, parse_analysis


@dataclass
class FakeMailReader:
    messages: list[MailSummary] = field(default_factory=list)
    error: Exception | None = None
    calls: int = 0

    async def fetch_inbox(
        self,
        *,
        query: str | None = None,
        limit: int | None = None,
        unread_only: bool = False,
        folder: str = "INBOX",
    ) -> list[MailSummary]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        items = list(self.messages)
        if limit is not None:
            items = items[:limit]
        return items


@dataclass
class FakeAnalyzer:
    notable: bool = True
    memory_worthy: bool = False
    analyses: list[str] = field(default_factory=list)

    async def analyze(
        self,
        *,
        observation_id: UUID,
        sender: str,
        subject: str,
        snippet: str,
        prompt: str,
    ) -> MailAnalysis:
        self.analyses.append(subject)
        return MailAnalysis(
            summary=f"{subject}的摘要",
            notable=self.notable,
            memory_worthy=self.memory_worthy,
            topic=f"你有一封关于{subject}的邮件",
        )


@dataclass
class FakeTimeline:
    events: list[dict[str, Any]] = field(default_factory=list)

    async def index_custom(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


@dataclass
class FakePerception:
    events: list[SemanticEvent] = field(default_factory=list)

    def submit(
        self,
        event: SemanticEvent,
        *,
        stable_for_seconds: float,
        validate: Any = None,
        handler: Any = None,
    ) -> None:
        self.events.append(event)


class _Session:
    owner_id: UUID | None = None

    async def scalar(self, query: Any) -> Any:
        return type(self).owner_id


class _Sessions:
    def __call__(self) -> _Sessions:
        return self

    async def __aenter__(self) -> _Session:
        return _Session()

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeDatabase:
    sessions = _Sessions()


@dataclass
class _ConfigView:
    config: HubConfig


def _hub_config(mail_awareness: MailAwarenessConfig) -> HubConfig:
    base = yaml.safe_load(
        """
models:
  cloud:
    provider: openai_compatible
    model: dialogue-v1
    base_url: https://models.example/v1
    secret_value: test
    runs_local: false
    max_privacy_level: L1
  local_private:
    provider: openai_compatible
    model: qwen-local
    base_url: http://127.0.0.1:1234/v1
    secret_value: local
    runs_local: true
    max_privacy_level: L3
routes:
  dialogue: {primary: cloud, fallbacks: []}
  utility: {primary: cloud, fallbacks: []}
  private: {primary: local_private, fallbacks: []}
"""
    )
    base["mail_awareness"] = mail_awareness.model_dump(mode="json")
    return HubConfig.model_validate(base)


def _loop(
    reader: FakeMailReader,
    analyzer: FakeAnalyzer | None = None,
    *,
    perception: FakePerception | None = None,
    config: MailAwarenessConfig | None = None,
    owner_id: UUID | None = None,
) -> tuple[MailAwarenessLoop, FakeTimeline]:
    _Session.owner_id = owner_id or UUID("00000000-0000-7000-8000-000000000001")
    timeline = FakeTimeline()
    config_view = cast(
        Any, _ConfigView(_hub_config(config or MailAwarenessConfig(enabled=True)))
    )
    loop = MailAwarenessLoop(
        config_store=config_view,
        database=FakeDatabase(),
        reader=reader,
        analyzer=analyzer or FakeAnalyzer(),
        timeline=timeline,  # type: ignore[arg-type]
        perception_pipeline=perception,
    )
    return loop, timeline


def _summary(uid: int, subject: str, sender: str = "noreply@example.com") -> MailSummary:
    return MailSummary(
        uid=uid,
        sender=sender,
        subject=subject,
        sent_at=datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
        snippet="片段内容",
        unread=True,
    )


ENABLED = MailAwarenessConfig(enabled=True)


async def test_first_tick_builds_baseline_without_notifying() -> None:
    perception = FakePerception()
    loop, timeline = _loop(FakeMailReader([_summary(10, "旧账单")]), perception=perception)

    await loop._tick(ENABLED)
    await loop._tick(ENABLED)

    assert timeline.events == []
    assert perception.events == []


async def test_new_mail_is_analyzed_indexed_and_submitted() -> None:
    perception = FakePerception()
    reader = FakeMailReader()
    analyzer = FakeAnalyzer()
    loop, timeline = _loop(reader, analyzer, perception=perception)

    await loop._tick(ENABLED)
    reader.messages = [_summary(10, "旧账单"), _summary(11, "面试邀约")]
    await loop._tick(ENABLED)

    assert analyzer.analyses == ["旧账单", "面试邀约"]
    assert [event["title"] for event in timeline.events] == [
        "邮件：旧账单",
        "邮件：面试邀约",
    ]
    assert [event.kind for event in perception.events] == ["mail.received", "mail.received"]
    assert perception.events[0].dedupe_key == "mail:10"
    assert perception.events[0].source_kind == "mail"
    assert perception.events[0].privacy_level == "L1"


async def test_tick_respects_max_messages_per_tick() -> None:
    perception = FakePerception()
    reader = FakeMailReader()
    loop, _ = _loop(reader, perception=perception)
    cfg = MailAwarenessConfig(enabled=True, max_messages_per_tick=1)

    await loop._tick(cfg)
    reader.messages = [_summary(10, "a"), _summary(11, "b"), _summary(12, "c")]
    await loop._tick(cfg)
    assert len(perception.events) == 1

    # watermark 只推进已处理的 UID：剩余的留到下一 tick
    await loop._tick(cfg)
    assert len(perception.events) == 2


async def test_disabled_and_cooldown_skip_fetch() -> None:
    reader = FakeMailReader([_summary(10, "x")])
    loop, _ = _loop(reader)
    await loop._tick(MailAwarenessConfig(enabled=False))
    assert reader.calls == 0

    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    loop._clock = lambda: now
    loop.state.cooldown_until = now + timedelta(minutes=5)
    await loop._tick(ENABLED)
    assert reader.calls == 0


async def test_mail_error_counts_failures_into_cooldown() -> None:
    reader = FakeMailReader(error=MailError("mail_not_configured"))
    loop, _ = _loop(reader)

    for _ in range(3):
        await loop._tick(ENABLED)

    assert loop.state.cooldown_until is not None
    assert loop.state.last_error == "mail_not_configured"


async def test_notable_false_stays_silent_but_indexed() -> None:
    perception = FakePerception()
    reader = FakeMailReader()
    analyzer = FakeAnalyzer(notable=False)
    loop, timeline = _loop(reader, analyzer, perception=perception)

    await loop._tick(ENABLED)
    reader.messages = [_summary(10, "验证码")]
    await loop._tick(ENABLED)

    assert perception.events == []
    assert len(timeline.events) == 1


def test_parse_analysis_extracts_fields() -> None:
    analysis = parse_analysis(
        '前言 {"summary":"账单到期","notable":true,"memory_worthy":false,"topic":"记得交电费"} 后言'
    )
    assert analysis.summary == "账单到期"
    assert analysis.notable is True
    assert analysis.memory_worthy is False
    assert analysis.topic == "记得交电费"

    fallback = parse_analysis("没有 JSON 的输出")
    assert fallback.summary == "没有 JSON 的输出"
    assert fallback.notable is False
