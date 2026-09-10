from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status

from app.integrations.mcp import McpManager, McpManagerError
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard


class McpServerStatusItem(StrictModel):
    server_id: str
    configured_enabled: bool
    available: bool
    refreshing: bool
    protocol_version: str | None
    server_name: str | None
    server_version: str | None
    tool_count: int
    last_refresh_at: datetime | None
    last_error: str | None
    consecutive_failures: int


class McpServerListResponse(StrictModel):
    enabled: bool
    servers: list[McpServerStatusItem]


class McpToolItem(StrictModel):
    internal_name: str
    server_id: str
    remote_name: str
    title: str
    description: str
    input_schema: dict[str, object]
    read_only: bool
    destructive: bool
    idempotent: bool


class McpToolListResponse(StrictModel):
    items: list[McpToolItem]
    total: int


def create_admin_mcp_router(
    manager: McpManager | None, *, admin_token: str | None
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/mcp",
        tags=["admin-mcp"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("/servers", response_model=McpServerListResponse)
    async def servers() -> McpServerListResponse:
        if manager is None:
            return McpServerListResponse(enabled=False, servers=[])
        return McpServerListResponse(
            enabled=manager.enabled,
            servers=[
                McpServerStatusItem(
                    server_id=item.server_id,
                    configured_enabled=item.configured_enabled,
                    available=item.available,
                    refreshing=item.refreshing,
                    protocol_version=item.protocol_version,
                    server_name=item.server_name,
                    server_version=item.server_version,
                    tool_count=item.tool_count,
                    last_refresh_at=item.last_refresh_at,
                    last_error=item.last_error,
                    consecutive_failures=item.consecutive_failures,
                )
                for item in manager.states
            ],
        )

    @router.get("/tools", response_model=McpToolListResponse)
    async def tools() -> McpToolListResponse:
        catalog = manager.catalog() if manager is not None else ()
        items = [
            McpToolItem(
                internal_name=item.internal_name,
                server_id=item.server_id,
                remote_name=item.remote_name,
                title=item.title,
                description=item.description,
                input_schema=item.input_schema,
                read_only=item.read_only,
                destructive=item.destructive,
                idempotent=item.idempotent,
            )
            for item in catalog
        ]
        return McpToolListResponse(items=items, total=len(items))

    @router.post("/servers/{server_id}/refresh", response_model=McpServerStatusItem)
    async def refresh(server_id: str) -> McpServerStatusItem:
        if manager is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="mcp_manager_unavailable",
            )
        try:
            item = await manager.refresh_server(server_id)
        except McpManagerError as error:
            code = (
                status.HTTP_404_NOT_FOUND
                if error.reason_code == "mcp_server_not_found"
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=code, detail=error.reason_code) from error
        return McpServerStatusItem(
            server_id=item.server_id,
            configured_enabled=item.configured_enabled,
            available=item.available,
            refreshing=item.refreshing,
            protocol_version=item.protocol_version,
            server_name=item.server_name,
            server_version=item.server_version,
            tool_count=item.tool_count,
            last_refresh_at=item.last_refresh_at,
            last_error=item.last_error,
            consecutive_failures=item.consecutive_failures,
        )

    return router
