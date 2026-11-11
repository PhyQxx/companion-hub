"""Non-secret process identity frozen when a maintenance consumer starts."""

import os
from typing import Any

from app.build_info import build_info


def worker_identity(worker_id: str, resource_class: str) -> dict[str, Any]:
    build = build_info()
    return {
        "schema_version": 1,
        "worker_id": worker_id,
        "resource_class": resource_class,
        "pid": os.getpid(),
        "build": {
            key: value[:160] if isinstance(value, str) else None
            for key in ("version", "revision", "source_digest", "built_at")
            for value in (build.get(key),)
        },
    }
