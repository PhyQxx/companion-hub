"""记忆复用归因（方案 C2/03B 增强）：判定回答或工具参数是否真的复用了一条记忆。

语义合同（docs/记忆分级与遗忘方案 §5）：
- 归因必须基于可验证的文本复用，检索/注入曝光从不计强化；
- 宁可少计：匹配要求显著高于"碰巧共现"——归一化整段包含，或至少
  4 个连续字符的片段在目标文本中出现（4 字片段对个人事实已足够
  区分；孤立 1~3 字重合如"香菜"不算复用）；
- 证据太短（归一化后 <6 字）不足以视为可靠复用信号。

纯函数、无 IO；chat 回答归因与只读工具参数归因共用同一口径。
"""

from __future__ import annotations

import re

# 归一化：小写并去掉空白/标点（保留字母、数字与 CJK，\w 含 Unicode 字母）。
_NORMALIZE = re.compile(r"[\W_]+", re.UNICODE)
# 视为可靠复用的最小连续片段长度（宁可少计的保守下限）。
MIN_DISTINCTIVE_SPAN = 4
# 证据本身的可靠长度下限：过短的"事实"缺乏区分度。
MIN_EVIDENCE_LENGTH = 6


def normalize_evidence(text: str) -> str:
    return _NORMALIZE.sub("", text.lower())


def evidence_used_in_text(
    evidence: str,
    text: str,
    *,
    min_length: int = MIN_EVIDENCE_LENGTH,
    span: int = MIN_DISTINCTIVE_SPAN,
) -> bool:
    """记忆证据是否被目标文本（回答正文 / 工具参数值）复用。

    判定口径（按保守程度递进，任一命中即算复用）：
    1. 归一化整段包含——标点/空格差异不影响；
    2. 归一化证据的任一 `span` 字连续片段出现在目标文本——同义改写
       通常保留事实核心片段（"不吃香菜""去掉香菜""5月6日"）。
    归一化后不足 min_length 的证据一律 False（宁少计）。
    """
    normalized_evidence = normalize_evidence(evidence)
    if len(normalized_evidence) < min_length:
        return False
    normalized_text = normalize_evidence(text)
    if normalized_evidence in normalized_text:
        return True
    evidence_grams = {
        normalized_evidence[index : index + span]
        for index in range(len(normalized_evidence) - span + 1)
    }
    if not evidence_grams:
        return False
    return any(gram in normalized_text for gram in evidence_grams)
