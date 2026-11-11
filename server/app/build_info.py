"""Public deployment identity, independent of credentials or runtime configuration."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from app import __version__


def build_info() -> dict[str, str | None]:
    path = Path(os.getenv("ARIA_BUILD_INFO_PATH", "/app/build-info.json"))
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    revision = data.get("revision")
    digest = data.get("source_digest")
    identity = revision or digest or "dev"
    return {
        "version": f"{__version__}+{str(identity)[:12]}",
        "revision": revision,
        "source_digest": digest,
        "built_at": data.get("built_at"),
        "deployed_at": os.getenv("ARIA_DEPLOYED_AT"),
        "started_at": os.getenv("ARIA_STARTED_AT") or datetime.now(UTC).isoformat(),
    }
