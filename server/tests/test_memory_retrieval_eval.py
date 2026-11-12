"""检索质量门禁（方案 B2）：合成考题集的下限回归。

跑分与完整基线见 scripts/eval_memory_retrieval.py 与
docs/baselines/memory-retrieval-eval.json；本测试只锁定防退化下限
（略低于当前基线，正常抖动不误报，真实退化必失败）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.db import Database
from scripts.eval_memory_retrieval import run_eval


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    from test_run_cancel_fence import prepared

    storage = await prepared("sqlite", tmp_path)
    try:
        yield storage.database
    finally:
        await storage.close()


async def test_retrieval_eval_regression_floors(database: Database) -> None:
    report = await run_eval(database)

    overall = report["overall"]
    assert overall["privacy_violations"] == 0, report["violations"]
    assert overall["cross_user_leaks"] == 0
    assert overall["recall_at_8"] >= 0.75
    category = {name: bucket["recall_at_8"] for name, bucket in report["per_category"].items()}
    # 精确槽位（含 dormant 冷层）与深搜池覆盖是硬契约。
    assert category["exact_cold"] >= 0.85
    assert category["cold_deep"] >= 1.0
    assert category["inventory"] >= 1.0
    assert category["privacy_boundary"] >= 1.0
    # 干扰项在场时正确答案仍应第一（相关性先于等级）。
    assert report["per_category"]["distractor"]["top1"] >= 0.60
    assert report["per_category"]["exact_cold"]["top1"] >= 0.80
