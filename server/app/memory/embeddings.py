# ruff: noqa: RUF001
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Protocol

_TOKEN_SPLIT = re.compile(r"[\s,.;:!?，。；：！？、()\[\]（）【】\"'“”‘’]+")


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    @property
    def version(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbeddingProvider:
    """Deterministic local embedding over character n-grams.

    Runs fully offline, keeps memory retrieval testable without network access
    and records its own version so vectors never mix across providers. The
    pgvector + utility-model upgrade replaces this behind the same protocol.
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
    if len(left) != len(right):
        raise ValueError("embedding dimensions do not match")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


def lexical_cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(count * right.get(token, 0) for token, count in left.items())
    norm_left = math.sqrt(sum(count * count for count in left.values()))
    norm_right = math.sqrt(sum(count * count for count in right.values()))
    return dot / (norm_left * norm_right)


def text_tokens(text: str) -> Counter[str]:
    return Counter(_ngrams(text))
