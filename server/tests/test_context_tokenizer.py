import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_context_window import endpoint, request
from test_llm import FakeProvider
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

from app.harness.calibration import CalibrationCorpus, calibrate
from app.harness.tokenizer import ContextTokenizerUnavailable, count_tokens
from app.harness.window import ContextWindowExceeded, fit_window
from app.llm.contracts import (
    ContextTokenizer,
    LLMMessage,
    LLMRoute,
    RoutePolicy,
    ToolCall,
    ToolDefinition,
)
from app.llm.router import LLMRouteExhausted, LLMRouter


@pytest.fixture
def native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContextTokenizer:
    tokenizer = Tokenizer(
        WordLevel({"[UNK]": 0, "hello": 1, "world": 2, "你好": 3}, unk_token="[UNK]")
    )
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.enable_truncation(max_length=2)
    tokenizer.enable_padding(length=128)
    payload = tokenizer.to_str().encode()
    (tmp_path / "synthetic.json").write_bytes(payload)
    monkeypatch.setenv("ARIA_TOKENIZER_DIR", str(tmp_path))
    return ContextTokenizer(
        id="synthetic", sha256=hashlib.sha256(payload).hexdigest(), safety_multiplier=1
    )


def test_native_count_disables_artifact_truncation_and_padding(native: ContextTokenizer) -> None:
    assert count_tokens("hello world 你好", native) == 3
    assert count_tokens("hello " * 50, native) == 50


def test_native_window_keeps_current_tool_chain_and_declares_protocol_uncertainty(
    native: ContextTokenizer,
) -> None:
    call = ToolCall(id="fixture", function={"name": "lookup", "arguments": {}})
    original = request(
        [
            LLMMessage(role="system", content="事实 987654321"),
            LLMMessage(role="user", content="hello world 你好"),
            LLMMessage(role="assistant", tool_calls=[call]),
            LLMMessage(
                role="tool", name="lookup", tool_call_id="fixture", content="synthetic result"
            ),
        ]
    ).model_copy(
        update={
            "tools": [
                ToolDefinition(
                    name="lookup", description="synthetic", parameters={"type": "object"}
                )
            ]
        }
    )
    fitted = fit_window(original, endpoint(1000).model_copy(update={"context_tokenizer": native}))
    assert fitted.request.messages == original.messages
    assert fitted.manifest["estimator"] == "native_json_estimate_v1"
    assert fitted.manifest["tokenizer_sha256"] == native.sha256
    assert fitted.manifest["provider_protocol_validation"] == "unverified"
    assert "987654321" not in json.dumps(fitted.manifest)


def test_native_protected_context_still_refuses_overflow(native: ContextTokenizer) -> None:
    with pytest.raises(ContextWindowExceeded):
        fit_window(
            request([LLMMessage(role="user", content="hello " * 2000)]),
            endpoint(1000).model_copy(update={"context_tokenizer": native}),
        )


def test_native_old_turns_trim_as_whole_units(native: ContextTokenizer) -> None:
    call = ToolCall(id="fixture", function={"name": "lookup", "arguments": {}})
    original = request(
        [
            LLMMessage(role="system", content="事实 987654321"),
            LLMMessage(role="user", content="hello " * 1000),
            LLMMessage(role="assistant", tool_calls=[call]),
            LLMMessage(role="tool", name="lookup", tool_call_id="fixture", content="old result"),
            LLMMessage(role="user", content="hello world"),
        ]
    )
    fitted = fit_window(original, endpoint(1000).model_copy(update={"context_tokenizer": native}))
    assert fitted.request.messages == [original.messages[0], original.messages[-1]]
    assert fitted.manifest["excluded_messages"] == 3


@pytest.mark.parametrize("mode", ["missing", "fingerprint", "symlink", "malformed", "fifo"])
def test_artifacts_fail_closed_without_reading_arbitrary_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    path = tmp_path / "fixture.json"
    payload = b"invalid synthetic tokenizer"
    if mode == "symlink":
        other = tmp_path / "other"
        other.write_bytes(payload)
        path.symlink_to(other)
    elif mode == "fifo":
        os.mkfifo(path)
    elif mode != "missing":
        path.write_bytes(payload)
    monkeypatch.setenv("ARIA_TOKENIZER_DIR", str(tmp_path))
    spec = ContextTokenizer(
        id="fixture",
        sha256=("0" * 64 if mode == "fingerprint" else hashlib.sha256(payload).hexdigest()),
    )
    with pytest.raises(ContextTokenizerUnavailable):
        count_tokens("hello", spec)


def test_native_requires_directory_and_safe_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARIA_TOKENIZER_DIR", raising=False)
    with pytest.raises(ContextTokenizerUnavailable, match="directory_missing"):
        count_tokens("hello", ContextTokenizer(id="fixture", sha256="0" * 64))
    with pytest.raises(ValidationError):
        ContextTokenizer(id="../../other", sha256="0" * 64)


@pytest.mark.parametrize("stream", [False, True])
async def test_router_skips_invalid_native_primary_before_provider_and_refits_fallback(
    native: ContextTokenizer, stream: bool
) -> None:
    bad = endpoint(1000).model_copy(
        update={"context_tokenizer": native.model_copy(update={"sha256": "0" * 64})}
    )
    primary, fallback = FakeProvider("primary"), FakeProvider("fallback")
    router = LLMRouter(
        endpoints={"primary": bad, "fallback": endpoint(1000)},
        providers={"primary": primary, "fallback": fallback},
        routes={name: RoutePolicy(primary="primary", fallbacks=["fallback"]) for name in LLMRoute},
    )
    original = request([LLMMessage(role="user", content="hello")])
    if stream:

        async def delta(value: str) -> None:
            pass

        result = await router.stream(original, delta)
    else:
        result = await router.complete(original)
    assert primary.requests == [] and len(fallback.requests) == 1
    assert result.context_budget["estimator"] == "utf8_bytes_v1"
    only_bad = LLMRouter(
        endpoints={"primary": bad},
        providers={"primary": primary},
        routes={name: RoutePolicy(primary="primary") for name in LLMRoute},
    )
    with pytest.raises(LLMRouteExhausted, match="context_tokenizer_unavailable"):
        await only_bad.complete(original)


def corpus(prompt_tokens: int) -> CalibrationCorpus:
    return CalibrationCorpus.model_validate(
        {
            "version": 1,
            "synthetic": True,
            "provider": "fixture",
            "model": "synthetic",
            "cases": [
                {
                    "id": "mixed",
                    "messages": [
                        {"role": "user", "content": "hello world 你好 synthetic-private-marker"}
                    ],
                    "prompt_tokens": prompt_tokens,
                    "usage_known": True,
                }
            ],
        }
    )


def test_calibration_reports_undercount_without_content_or_semantic_claim(
    native: ContextTokenizer,
) -> None:
    report = calibrate(corpus(1000), native)
    assert report["all_cases_within_estimate"] is False
    assert int(str(report["max_underestimate_tokens"])) > 0
    assert report["usage_provenance"] == "operator_declared_synthetic_fixture"
    assert report["provider_protocol_validation"] == "unverified"
    assert "synthetic-private-marker" not in json.dumps(report)
    assert calibrate(corpus(10), native)["all_cases_within_estimate"] is True


def test_calibration_requires_synthetic_unique_known_usage() -> None:
    original = corpus(10).model_dump()
    for update in (
        {"synthetic": False},
        {"cases": original["cases"] * 2},
        {"cases": [{**original["cases"][0], "usage_known": False}]},
    ):
        with pytest.raises(ValidationError):
            CalibrationCorpus.model_validate({**original, **update})


def test_calibration_cli_is_offline_and_fails_on_undercount(
    native: ContextTokenizer, tmp_path: Path
) -> None:
    path = tmp_path / "corpus.json"
    path.write_text(corpus(1000).model_dump_json())
    result = subprocess.run(
        [
            sys.executable,
            "server/scripts/calibrate_tokenizer.py",
            "--corpus",
            str(path),
            "--tokenizer-id",
            native.id,
            "--tokenizer-sha256",
            native.sha256,
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["all_cases_within_estimate"] is False
    assert "synthetic-private-marker" not in result.stdout + result.stderr
    path.write_text('{"synthetic":false,"private":"synthetic-private-marker"}')
    invalid = subprocess.run(
        [
            sys.executable,
            "server/scripts/calibrate_tokenizer.py",
            "--corpus",
            str(path),
            "--tokenizer-id",
            native.id,
            "--tokenizer-sha256",
            native.sha256,
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert invalid.returncode == 2
    assert "synthetic-private-marker" not in invalid.stdout + invalid.stderr


def test_oversized_artifact_is_rejected_before_decode(
    native: ContextTokenizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.harness.tokenizer.MAX_ARTIFACT_BYTES", 8)
    with pytest.raises(ContextTokenizerUnavailable, match="artifact_invalid"):
        count_tokens("hello", native)


async def test_native_failure_cannot_escape_private_route(native: ContextTokenizer) -> None:
    bad = endpoint(1000).model_copy(
        update={"context_tokenizer": native.model_copy(update={"sha256": "0" * 64})}
    )
    cloud = endpoint(1000).model_copy(update={"runs_local": False, "max_privacy_level": "L1"})
    primary, fallback = FakeProvider("primary"), FakeProvider("cloud")
    router = LLMRouter(
        endpoints={"primary": bad, "cloud": cloud},
        providers={"primary": primary, "cloud": fallback},
        routes={
            name: RoutePolicy(
                primary="primary", fallbacks=[] if name == LLMRoute.PRIVATE else ["cloud"]
            )
            for name in LLMRoute
        },
    )
    original = request([LLMMessage(role="user", content="synthetic private input")]).model_copy(
        update={"privacy_level": "L2"}
    )
    with pytest.raises(LLMRouteExhausted):
        await router.complete(original)
    assert primary.requests == fallback.requests == []
