from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def _load_env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        return value.strip().strip("\"").strip("'") or None
    return None


def _request_json(
    url: str,
    *,
    token: str,
    method: str = "GET",
    payload: object | None = None,
) -> dict[str, object]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=10) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError("admin config API returned a non-object response")
    return value


def _route_summary(config: dict[str, object]) -> str:
    routes = config.get("routes")
    models = config.get("models")
    if not isinstance(routes, dict) or not isinstance(models, dict):
        raise RuntimeError("admin config response is missing routes/models")
    dialogue = routes.get("dialogue")
    if not isinstance(dialogue, dict):
        raise RuntimeError("admin config response is missing dialogue route")
    names = [dialogue.get("primary"), *dialogue.get("fallbacks", [])]
    parts: list[str] = []
    for name in names:
        if not isinstance(name, str):
            continue
        endpoint = models.get(name)
        is_local = isinstance(endpoint, dict) and endpoint.get("runs_local") is True
        parts.append(f"{name}{' [local]' if is_local else ' [cloud]'}")
    return " -> ".join(parts)


def _remove_local_dialogue_fallbacks(config: dict[str, object]) -> list[str]:
    routes = config.get("routes")
    models = config.get("models")
    if not isinstance(routes, dict) or not isinstance(models, dict):
        raise RuntimeError("admin config response is missing routes/models")
    dialogue = routes.get("dialogue")
    if not isinstance(dialogue, dict):
        raise RuntimeError("admin config response is missing dialogue route")
    fallbacks = dialogue.get("fallbacks")
    if not isinstance(fallbacks, list):
        raise RuntimeError("dialogue route fallbacks must be a list")

    removed: list[str] = []
    kept: list[object] = []
    for name in fallbacks:
        endpoint = models.get(name) if isinstance(name, str) else None
        if isinstance(endpoint, dict) and endpoint.get("runs_local") is True:
            removed.append(name)
        else:
            kept.append(name)
    dialogue["fallbacks"] = kept
    return removed


def _ensure_cloud_dialogue_fallback(config: dict[str, object], name: str) -> bool:
    routes = config.get("routes")
    models = config.get("models")
    if not isinstance(routes, dict) or not isinstance(models, dict):
        raise RuntimeError("admin config response is missing routes/models")
    dialogue = routes.get("dialogue")
    if not isinstance(dialogue, dict):
        raise RuntimeError("admin config response is missing dialogue route")
    fallbacks = dialogue.get("fallbacks")
    if not isinstance(fallbacks, list):
        raise RuntimeError("dialogue route fallbacks must be a list")

    endpoint = models.get(name)
    if not isinstance(endpoint, dict):
        raise RuntimeError(f"dialogue fallback endpoint does not exist: {name}")
    if endpoint.get("runs_local") is True:
        raise RuntimeError(f"dialogue fallback must be cloud-hosted: {name}")
    if dialogue.get("primary") == name:
        raise RuntimeError(f"dialogue fallback is already the primary endpoint: {name}")

    next_fallbacks = [name, *(item for item in fallbacks if item != name)]
    changed = next_fallbacks != fallbacks
    dialogue["fallbacks"] = next_fallbacks
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or make the live dialogue route cloud-only via localhost admin API."
    )
    parser.add_argument("--apply", action="store_true", help="Publish the adjusted config.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--ensure-fallback",
        help="Ensure this cloud endpoint is the first dialogue fallback.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    token = os.getenv("ARIA_ADMIN_TOKEN") or _load_env_value(
        root / ".env.local",
        "ARIA_ADMIN_TOKEN",
    )
    if not token:
        print("ARIA_ADMIN_TOKEN is not available in the environment or .env.local")
        return 2

    current_url = f"{args.base_url.rstrip('/')}/api/v1/admin/config/current"
    try:
        current = _request_json(current_url, token=token)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        print(f"failed to read live config: {type(error).__name__}: {error}")
        return 1

    version = current.get("version")
    config = current.get("config")
    if not isinstance(config, dict):
        print("live config response did not include a config object")
        return 1

    print(f"current config version: {version}")
    print(f"before: {_route_summary(config)}")
    try:
        removed = _remove_local_dialogue_fallbacks(config)
        ensured = bool(args.ensure_fallback) and _ensure_cloud_dialogue_fallback(
            config,
            args.ensure_fallback,
        )
    except RuntimeError as error:
        print(f"failed to adjust live config: {error}")
        return 1

    if not removed and not ensured:
        print("dialogue route already satisfies requested cloud-only policy")
        return 0
    if removed:
        print(f"remove local dialogue fallback(s): {', '.join(removed)}")
    if ensured:
        print(f"promote cloud dialogue fallback: {args.ensure_fallback}")
    print(f"after:  {_route_summary(config)}")

    if not args.apply:
        print("dry run only; pass --apply to publish")
        return 0

    try:
        updated = _request_json(current_url, token=token, method="PUT", payload=config)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        print(f"failed to publish live config: {type(error).__name__}: {error}")
        return 1
    updated_config = updated.get("config")
    if not isinstance(updated_config, dict):
        print("updated config response did not include a config object")
        return 1
    print(f"published config version: {updated.get('version')}")
    print(f"live:   {_route_summary(updated_config)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
