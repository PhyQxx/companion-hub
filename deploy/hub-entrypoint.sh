#!/bin/sh
set -eu

export ARIA_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export ARIA_DEPLOYED_AT="${ARIA_DEPLOYED_AT:-$ARIA_STARTED_AT}"

uv run --no-sync alembic upgrade head
exec uv run --no-sync uvicorn app.main:app --app-dir server --host 0.0.0.0 --port 8000
