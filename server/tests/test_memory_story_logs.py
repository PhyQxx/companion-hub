"""业主可读中文日志（方案批次八）：预览开关、逐条去向、回合总结。

约定变更（如实登记）：旧"诊断日志不落原文"放宽为默认 ≤40 字预览，
``ARIA_MEMORY_LOG_PREVIEW=0`` 可整体隐藏（分享日志前建议关闭）。
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.memory.logfmt import content_preview, decision_label, tier_label


def test_labels_are_chinese() -> None:
    assert tier_label("core") == "核心" and tier_label("dormant") == "冷存档"
    assert tier_label(None) == "未知"
    assert decision_label("created") == "新增"
    assert decision_label("supported").startswith("佐证")


def test_preview_truncates_and_hides_by_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    long_text = "用户的女儿可乐的生日是2024年4月13日，家人计划一起庆祝" * 3
    preview = content_preview(long_text)
    assert preview.endswith("…") and len(preview) <= 41
    assert content_preview("短内容") == "短内容"
    monkeypatch.setenv("ARIA_MEMORY_LOG_PREVIEW", "0")
    assert content_preview(long_text) == "（预览已隐藏）"
    assert content_preview(None) == "（空）"


@pytest.fixture(params=["sqlite", "postgresql"])
async def database(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[object]:
    from test_run_cancel_fence import prepared

    storage = await prepared(request.param, tmp_path)
    try:
        yield storage.database
    finally:
        await storage.close()


async def test_ingest_and_turn_summary_logged_chinese(
    database: object, caplog: pytest.LogCaptureFixture
) -> None:
    from datetime import UTC, datetime

    from test_goal_delivery_runs import seed
    from test_memory import candidate

    from app.memory import ConsolidateDecision, MemoryIngester, MemoryStore
    from app.schemas import PrivacyLevel

    owner, _, _ = await seed(database)
    ingester = MemoryIngester(MemoryStore(database))
    with caplog.at_level("INFO", logger="app.memory.consolidation"):
        first = await ingester.ingest(
            candidate(
                "用户的女儿小晴生日是2024年5月6日",
                fact_key="person.小晴.birthday",
                importance=0.9,
                confidence=0.9,
                initial_tier="core",
            ),
            user_id=owner,
        )
        from app.memory import MemoryType

        await ingester.ingest(
            candidate("用户不喜欢吃合成香菜", type=MemoryType.PREFERENCE), user_id=owner
        )
    stories = [record.getMessage() for record in caplog.records if "memory.story" in record.message]
    assert any("记忆#" in line and "新增" in line and "核心" in line for line in stories), stories

    # 回合总结行：规则提取器吃第一人称句子，ingest_turn 末尾输出总结。
    await ingester.ingest_turn(
        user_id=owner,
        user_message_id=first.memory.id,
        user_text="我不喜欢吃合成香菜",  # 规则改写后与已存内容逐字一致 → 佐证
        user_occurred_at=datetime.now(UTC),
        assistant_message_id=first.memory.id,
        assistant_text="好的。",
        assistant_occurred_at=datetime.now(UTC),
        privacy_level=PrivacyLevel.L1,
    )
    summaries = [line for line in stories if "本回合记忆整理完成" in line] or [
        record.getMessage() for record in caplog.records if "本回合记忆整理完成" in record.message
    ]
    assert any("佐证1" in line for line in summaries), summaries

    # 中文标签映射完整性。
    assert decision_label(str(ConsolidateDecision.CONFLICT)).startswith("疑似冲突")
