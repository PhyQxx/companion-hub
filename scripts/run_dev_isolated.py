"""Start an explicitly isolated development database; never fall back to aria."""

import os
import subprocess
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

root = Path(__file__).resolve().parents[1]
env = {
    key: value
    for key, value in {**dotenv_values(root / ".env.local"), **os.environ}.items()
    if value is not None
}
url = env.get("ARIA_DEV_DATABASE_URL")
if not url or not (make_url(url).database or "").endswith("_dev"):
    raise SystemExit(
        "Set ARIA_DEV_DATABASE_URL to a dedicated database ending in _dev; no data is copied."
    )
if url == env.get("ARIA_DATABASE_URL"):
    raise SystemExit("Development database must differ from the shared/default database")
env["ARIA_DATABASE_URL"] = url
env["PYTHONPATH"] = str(root / "server")
subprocess.run([str(root / ".venv/bin/alembic"), "upgrade", "head"], cwd=root, env=env, check=True)
os.execve(
    str(root / ".venv/bin/python"),
    ["python", "-m", "uvicorn", "app.main:app", "--reload", "--port", "8001"],
    env,
)
