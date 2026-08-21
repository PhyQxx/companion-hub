# ruff: noqa: RUF001
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.request import Request, urlopen
from uuid import uuid4

SERVER_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SERVER_ROOT.parent
sys.path.insert(0, str(SERVER_ROOT))

from app.config import HubConfig  # noqa: E402
from app.llm import (  # noqa: E402
    CompletionRequest,
    EnvSecretProvider,
    LiteLLMProvider,
    LLMMessage,
    LLMRoute,
)
from app.schemas import PrivacyLevel  # noqa: E402


def _load_env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'") or None
    return None


def _fetch_config(base_url: str, token: str) -> HubConfig:
    request = Request(
        f"{base_url.rstrip('/')}/api/v1/admin/config/current",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        payload: Any = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("config"), dict):
        raise RuntimeError("admin config response is missing config")
    return HubConfig.model_validate(payload["config"])


def _benchmark_request() -> CompletionRequest:
    messages = [
        LLMMessage(role="system", content="你是小艾。用简短自然的中文回答。"),
        LLMMessage(role="user", content="你好"),
        LLMMessage(role="assistant", content="你好呀。"),
        LLMMessage(role="user", content="今天心情不错"),
        LLMMessage(role="assistant", content="听起来真好。"),
        LLMMessage(role="user", content="我们做个语音延迟测试"),
        LLMMessage(role="assistant", content="好，我准备好了。"),
        LLMMessage(role="user", content="请只回答你好"),
    ]
    return CompletionRequest(
        trace_id=uuid4(),
        messages=messages,
        privacy_level=PrivacyLevel.L1,
        route=LLMRoute.DIALOGUE,
        max_tokens=64,
        temperature=0,
    )


async def _benchmark_endpoint(
    name: str,
    config: HubConfig,
    *,
    timeout_seconds: float,
) -> dict[str, object]:
    endpoint = config.models[name]
    provider = LiteLLMProvider(name, endpoint, EnvSecretProvider())
    started = perf_counter()
    first_token_at: float | None = None

    async def on_delta(delta: str) -> None:
        nonlocal first_token_at
        if delta and first_token_at is None:
            first_token_at = perf_counter()

    try:
        async with asyncio.timeout(timeout_seconds):
            result = await provider.stream(_benchmark_request(), on_delta)
    except Exception as error:
        return {
            "endpoint": name,
            "model": endpoint.model,
            "ok": False,
            "error_type": type(error).__name__,
            "elapsed_ms": int((perf_counter() - started) * 1000),
        }
    finished = perf_counter()
    return {
        "endpoint": name,
        "model": endpoint.model,
        "ok": bool(result.text),
        "first_token_ms": (
            int((first_token_at - started) * 1000)
            if first_token_at is not None
            else None
        ),
        "total_ms": int((finished - started) * 1000),
        "output_chars": len(result.text),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark first-token latency for live dialogue endpoints."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    args = parser.parse_args()
    token = os.getenv("ARIA_ADMIN_TOKEN") or _load_env_value(
        PROJECT_ROOT / ".env.local", "ARIA_ADMIN_TOKEN"
    )
    if not token:
        print(json.dumps({"ok": False, "reason": "admin_token_unavailable"}))
        return 2
    config = await asyncio.to_thread(_fetch_config, args.base_url, token)
    route = config.routes[LLMRoute.DIALOGUE]
    names = list(dict.fromkeys([route.primary, *route.fallbacks]))
    reports = [
        await _benchmark_endpoint(name, config, timeout_seconds=args.timeout_seconds)
        for name in names
        if name in config.models and config.models[name].enabled
    ]
    reports.sort(
        key=lambda item: (
            item.get("ok") is not True,
            item.get("first_token_ms")
            if isinstance(item.get("first_token_ms"), int)
            else sys.maxsize,
        )
    )
    print(json.dumps({"ok": True, "results": reports}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
