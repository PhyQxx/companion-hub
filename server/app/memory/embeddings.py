# ruff: noqa: RUF001
"""嵌入提供方：记忆向量化的统一入口。

设计要点：
- 所有实现遵循同一协议（model / dimension / version / embed）；
- 向量按 version 隔离，切换嵌入模型时新旧向量互不参与比较，
  必须新增列双写回填后再切读（docs/00 §3.6）；
- HashingEmbeddingProvider 是确定性本地实现：离线可用、测试可复现，
  是 utility 嵌入模型上线前的默认方案。
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Protocol

# 中文友好的分词切分：按空白与常见中英文标点切开，再做字符 n-gram
_TOKEN_SPLIT = re.compile(r"[\s,.;:!?，。；：！？、()\[\]（）【】\"'“”‘’]+")


class EmbeddingProvider(Protocol):
    """嵌入提供方协议：检索与沉淀共用，任何实现必须自带版本标识。"""

    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    @property
    def version(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbeddingProvider:
    """基于字符 n-gram 哈希的确定性本地嵌入。

    工作方式：把文本拆成一元 + 二元字符 n-gram，用 blake2b 把每个
    n-gram 哈希到固定维度上累加，最后做 L2 归一化。相同文本得到相同
    向量，字面相近的文本余弦相似度较高。它不具备真正的语义泛化能力，
    但完全离线、零成本、可测试，切到真实嵌入模型时按 version 隔离即可。
    """

    def __init__(self, dimension: int = 256) -> None:
        self._dimension = dimension

    @property
    def model_name(self) -> str:
        return "builtin.hashing"

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def version(self) -> str:
        # 版本号变化意味着向量空间不兼容，检索会自动跳过旧版本向量
        return "char-ngram-hash/1"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_one(text) for text in texts]

    def embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimension
        for token, count in Counter(_ngrams(text)).items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest, "big") % self._dimension
            vector[index] += count
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector


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
