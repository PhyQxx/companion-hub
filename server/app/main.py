from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.events import create_event_router
from app.db import Database, create_database


def create_app(
    database: Database | None = None,
    *,
    enable_dev_endpoints: bool | None = None,
) -> FastAPI:
    database_url = os.getenv("ARIA_DATABASE_URL")
    runtime_database = database or (create_database(database_url) if database_url else None)
    owns_database = database is None and runtime_database is not None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_database and runtime_database is not None:
            await runtime_database.close()

    app = FastAPI(title="Aria Companion Hub", version=__version__, lifespan=lifespan)

    @app.get("/healthz", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/meta/protocol", tags=["system"])
    async def protocol() -> dict[str, object]:
        return {
            "protocol_version": 1,
            "supported_protocol_versions": [1],
            "schemas": ["aria.input-envelope/1", "aria.output-intent/1"],
        }

    dev_enabled = enable_dev_endpoints
    if dev_enabled is None:
        dev_enabled = os.getenv("ARIA_ENABLE_DEV_ENDPOINTS", "false").lower() == "true"
    if dev_enabled and runtime_database is not None:
        app.include_router(create_event_router(runtime_database))

    return app


app = create_app()
