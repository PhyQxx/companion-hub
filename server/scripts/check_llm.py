from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from app.llm import EnvSecretProvider, LiteLLMProvider, ModelEndpoint  # noqa: E402


async def main() -> None:
    base_url = os.environ["SENSENOVA_BASE_URL"].rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    endpoint = ModelEndpoint(
        provider="openai_compatible",
        model=os.environ["SENSENOVA_MODEL"],
        supports_json_mode=False,
        base_url=base_url,
        secret_ref="env:SENSENOVA_API_KEY",
        runs_local=False,
        max_privacy_level="L1",
        timeout_ms=int(os.getenv("SENSENOVA_TIMEOUT_MS", "60000")),
        max_retries=0,
        max_context_tokens=131_072,
        input_cost_per_million=0,
        output_cost_per_million=0,
    )
    provider = LiteLLMProvider("connectivity_check", endpoint, EnvSecretProvider())
    try:
        await provider.probe()
    except Exception as error:
        raise SystemExit(f"provider check failed: {type(error).__name__}") from None
    print(f"provider reachable: model={endpoint.model}")


if __name__ == "__main__":
    asyncio.run(main())
