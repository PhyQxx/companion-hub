"""Stamp immutable image metadata; source digest works without .git in Docker context."""

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "/app")
digest = hashlib.sha256(Path(__file__).read_bytes())
entrypoint = Path("/usr/local/bin/hub-entrypoint")
if entrypoint.is_file():
    digest.update(entrypoint.read_bytes())
for folder in ("server", "web/apps/dist", "config"):
    for path in sorted((root / folder).rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            digest.update(str(path.relative_to(root)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
for name in ("pyproject.toml", "uv.lock", "alembic.ini"):
    digest.update((root / name).read_bytes())
(root / "build-info.json").write_text(
    json.dumps(
        {
            "revision": os.environ.get("BUILD_REVISION") or None,
            "source_digest": digest.hexdigest()[:12],
            "built_at": datetime.now(UTC).isoformat(),
        }
    )
)
