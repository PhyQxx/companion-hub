"""记忆复用归因（方案 C2/03B 增强）：同义改写命中、巧合共现不命中。

合同：宁可少计——归一化整段包含或 4 字连续片段才计复用；孤立 1~3 字
重合（如"香菜"）、过短证据一律不计。chat 回答归因与只读工具参数归因
共用同一口径（evidence_used_in_text）。
"""

from app.memory.attribution import (
    MIN_DISTINCTIVE_SPAN,
    MIN_EVIDENCE_LENGTH,
    evidence_used_in_text,
    normalize_evidence,
)

# 典型事实证据（对应 memory.content 去掉"用户"前缀后的形态）。
EVIDENCE = "不吃香菜，点菜时需要去掉香菜"


def test_normalize_strips_punctuation_and_whitespace() -> None:
    assert normalize_evidence("不吃香菜， 点菜时需要去掉香菜。") == "不吃香菜点菜时需要去掉香菜"
    assert normalize_evidence("  Height: 172 CM ") == "height172cm"


def test_exact_substring_still_attributes() -> None:
    assert evidence_used_in_text(EVIDENCE, "好的，我记得您不吃香菜，点菜时需要去掉香菜。")
    assert evidence_used_in_text(EVIDENCE, "用户不吃香菜，点菜时需要去掉香菜")


def test_paraphrase_with_distinctive_span_attributes() -> None:
    """同义改写：整段不再原样出现，但保留 ≥4 字事实核心片段 → 计复用。"""
    assert evidence_used_in_text(EVIDENCE, "放心，我记着呢——您不吃香菜，我这就为您去掉。")
    assert evidence_used_in_text(EVIDENCE, "点菜时需要去掉香菜这一点我已经记下了。")
    assert evidence_used_in_text(EVIDENCE, "So you 不吃香菜, noted for ordering.")


def test_incidental_short_overlap_does_not_attribute() -> None:
    """宁少计：仅"香菜"两字重合、无 4 字连续片段 → 不计。"""
    assert not evidence_used_in_text(EVIDENCE, "今晚想吃香菜牛肉火锅吗")
    assert not evidence_used_in_text(EVIDENCE, "香菜是一种常见的调味蔬菜。")


def test_unrelated_answer_does_not_attribute() -> None:
    assert not evidence_used_in_text(EVIDENCE, "好的，我来帮您查询明天的天气。")
    assert not evidence_used_in_text(EVIDENCE, "")


def test_short_evidence_below_floor_never_attributes() -> None:
    assert not evidence_used_in_text("香菜", "您不吃香菜，点菜时需要去掉香菜")
    assert not evidence_used_in_text("", EVIDENCE)
    assert MIN_EVIDENCE_LENGTH == 6 and MIN_DISTINCTIVE_SPAN == 4


def test_birthday_fact_via_date_span() -> None:
    """日期型事实：改写保留完整日期串 → 计复用；仅提人名（2 字）不计。

    已知边界（如实登记）：共享日期前缀（如"2024年5月"）是 ≥4 字连续
    片段，会命中——实际风险受"记忆必须先被检索注入"门槛约束，且
    日期前缀重合多与事实相关；完全消除需按证据类型定语，留待校准。
    """
    evidence = "女儿小晴生日是2024年5月6日"
    assert evidence_used_in_text(evidence, "已为您在2024年5月6日安排生日提醒。")
    assert evidence_used_in_text(evidence, "2024年5月有不少活动安排。")
    assert not evidence_used_in_text(evidence, "小晴最近在学校表现怎么样")
