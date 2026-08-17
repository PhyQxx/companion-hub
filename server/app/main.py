from fastapi import FastAPI

from app import __version__


def create_app() -> FastAPI:
    app = FastAPI(title="Aria Companion Hub", version=__version__)

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

    return app


app = create_app()

