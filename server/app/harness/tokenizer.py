"""Explicit, fingerprinted local artifacts; no model discovery or downloads."""

import hashlib
import os
from functools import lru_cache
from pathlib import Path
from stat import S_ISREG

from tokenizers import Tokenizer

from app.llm.contracts import ContextTokenizer

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


class ContextTokenizerUnavailable(ValueError):
    pass


@lru_cache(maxsize=4)
def _load(root: str, identifier: str, fingerprint: str) -> Tokenizer:
    try:
        path = Path(root) / f"{identifier}.json"
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not S_ISREG(metadata.st_mode) or metadata.st_size > MAX_ARTIFACT_BYTES:
                raise ContextTokenizerUnavailable("context_tokenizer_artifact_invalid")
            payload = source.read(MAX_ARTIFACT_BYTES + 1)
        if len(payload) > MAX_ARTIFACT_BYTES or hashlib.sha256(payload).hexdigest() != fingerprint:
            raise ContextTokenizerUnavailable("context_tokenizer_fingerprint_mismatch")
        tokenizer = Tokenizer.from_str(payload.decode("utf-8"))
        # A supplied artifact's encode-time truncation must never shrink counts.
        tokenizer.no_truncation()
        tokenizer.no_padding()
        return tokenizer
    except ContextTokenizerUnavailable:
        raise
    except Exception as error:
        raise ContextTokenizerUnavailable("context_tokenizer_unavailable") from error


def local_tokenizer(spec: ContextTokenizer) -> Tokenizer:
    root = os.getenv("ARIA_TOKENIZER_DIR")
    if not root:
        raise ContextTokenizerUnavailable("context_tokenizer_directory_missing")
    return _load(str(Path(root).resolve()), spec.id, spec.sha256)


def count_tokens(value: str, spec: ContextTokenizer) -> int:
    tokenizer = local_tokenizer(spec)
    try:
        return len(tokenizer.encode(value, add_special_tokens=False).ids)
    except Exception as error:
        raise ContextTokenizerUnavailable("context_tokenizer_encoding_failed") from error
