"""Deterministic vector and lexical math, independent of embedding adapters."""

import math
import re
from collections import Counter
from collections.abc import Sequence

# Split common Chinese and Latin punctuation before character n-grams.
_TOKEN_SPLIT = re.compile(r"[\s,.;:!?，。；：！？、()\[\]（）【】\"'“”‘’]+")  # noqa: RUF001


def _ngrams(text: str) -> list[str]:
    """归一化文本后产出一元 + 二元字符 n-gram（过滤纯空白片段）。"""
    normalized = _TOKEN_SPLIT.sub(" ", text.strip().lower())
    if not normalized:
        return []
    grams: list[str] = []
    characters = list(normalized)
    grams.extend(characters)
    grams.extend(
        f"{characters[index]}{characters[index + 1]}" for index in range(len(characters) - 1)
    )
    return [gram for gram in grams if gram.strip()]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """两个向量的余弦相似度；维度不一致视为错误直接抛出。"""
    if len(left) != len(right):
        raise ValueError("embedding dimensions do not match")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


def lexical_cosine(left: Counter[str], right: Counter[str]) -> float:
    """词袋（n-gram 计数）余弦：混合检索的词法通道打分函数。"""
    if not left or not right:
        return 0.0
    dot = sum(count * right.get(token, 0) for token, count in left.items())
    norm_left = math.sqrt(sum(count * count for count in left.values()))
    norm_right = math.sqrt(sum(count * count for count in right.values()))
    return dot / (norm_left * norm_right)


def text_tokens(text: str) -> Counter[str]:
    """把文本转成 n-gram 计数，供词法通道复用。"""
    return Counter(_ngrams(text))
