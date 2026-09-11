from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import McpServerConfig

from .client import McpClientFactory, sdk_client_factory
from .models import McpCallResult, McpRemoteTool, McpServerState, McpToolDescriptor

logger = logging.getLogger("app.integrations.mcp")

MAX_CATALOG_PAGES = 100


class McpManagerError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class McpManager:
    def __init__(
        self,
        config_store: ConfigStore | DatabaseConfigStore,
        *,
        client_factory: McpClientFactory = sdk_client_factory,
        clock: Any = None,
    ) -> None:
        self._config_store = config_store
        self._client_factory = client_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._states: dict[str, McpServerState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # 目录刷新成功后的回调（MCP-D 用它把写工具同步进动作注册表）
        self._catalog_listener: Any | None = None

    def set_catalog_listener(self, listener: Any) -> None:
        self._catalog_listener = listener

    def _notify_catalog_changed(self) -> None:
        if self._catalog_listener is None:
            return
        try:
            self._catalog_listener()
        except Exception:
            logger.warning("MCP catalog listener failed", exc_info=True)

    def server_config(self, server_id: str) -> McpServerConfig:
        """公开读取某 Server 的运行配置（动作注册表读取超时等参数）。"""
        return self._server_config(server_id)

    @property
    def states(self) -> tuple[McpServerState, ...]:
        self._sync_configured_states()
        return tuple(self._states[key] for key in sorted(self._states))

    @property
    def enabled(self) -> bool:
        return self._config_store.current.config.mcp.enabled

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="aria-mcp-catalog")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self.refresh_expired()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=30)

    def _server_config(self, server_id: str) -> McpServerConfig:
        mcp = self._config_store.current.config.mcp
        for config in mcp.servers:
            if config.server_id == server_id:
                return config
        raise McpManagerError("mcp_server_not_found")

    def _sync_configured_states(self) -> None:
        config = self._config_store.current.config.mcp
        configured = {item.server_id: item for item in config.servers}
        for server_id, item in configured.items():
            state = self._states.setdefault(server_id, McpServerState(server_id=server_id))
            state.configured_enabled = config.enabled and item.enabled
            if not state.configured_enabled:
                state.available = False
                state.tools.clear()
                state.tool_count = 0
        for server_id in set(self._states) - set(configured):
            del self._states[server_id]

    async def refresh_expired(self) -> None:
        self._sync_configured_states()
        now = self._clock()
        for state in self.states:
            if not state.configured_enabled:
                continue
            config = self._server_config(state.server_id)
            if (
                state.last_refresh_at is None
                or now - state.last_refresh_at >= timedelta(seconds=config.catalog_ttl_seconds)
            ):
                with contextlib.suppress(Exception):
                    await self.refresh_server(state.server_id)

    async def refresh_all(self) -> None:
        self._sync_configured_states()
        for state in self.states:
            if state.configured_enabled:
                await self.refresh_server(state.server_id)

    async def refresh_server(self, server_id: str) -> McpServerState:
        self._sync_configured_states()
        config = self._server_config(server_id)
        state = self._states[server_id]
        if not state.configured_enabled:
            raise McpManagerError("mcp_server_disabled")
        lock = self._locks.setdefault(server_id, asyncio.Lock())
        async with lock:
            state.refreshing = True
            try:
                remote_tools: list[McpRemoteTool] = []
                cursor: str | None = None
                async with self._client_factory(config) as client:
                    for _ in range(MAX_CATALOG_PAGES):
                        page, cursor = await client.list_tools(cursor)
                        remote_tools.extend(page)
                        if cursor is None:
                            break
                    else:
                        raise McpManagerError("mcp_catalog_page_limit")
                    tools = self._map_tools(config, remote_tools)
                    state.protocol_version = client.protocol_version
                    state.server_name = client.server_name
                    state.server_version = client.server_version
                state.tools = {item.internal_name: item for item in tools}
                state.tool_count = len(tools)
                state.available = True
                state.last_refresh_at = self._clock()
                state.last_error = None
                state.consecutive_failures = 0
            except McpManagerError as error:
                state.available = False
                state.consecutive_failures += 1
                state.last_error = error.reason_code
                raise
            except Exception as error:
                state.available = False
                state.consecutive_failures += 1
                state.last_error = self._reason_for(error)
                logger.warning("MCP catalog refresh failed server_id=%s", server_id, exc_info=True)
            finally:
                state.refreshing = False
        if state.available:
            self._notify_catalog_changed()
        return state

    @staticmethod
    def _map_tools(
        config: McpServerConfig, remote_tools: list[McpRemoteTool]
    ) -> tuple[McpToolDescriptor, ...]:
        allowed = set(config.allowed_tools)
        seen: set[str] = set()
        mapped: list[McpToolDescriptor] = []
        for tool in remote_tools:
            if tool.name not in allowed:
                continue
            if tool.name in seen:
                raise McpManagerError("mcp_duplicate_remote_tool")
            seen.add(tool.name)
            read_only = tool.read_only_hint is True
            if not read_only and not config.allow_write_tools:
                continue
            mapped.append(
                McpToolDescriptor(
                    internal_name=f"mcp.{config.server_id}.{tool.name}",
                    server_id=config.server_id,
                    remote_name=tool.name,
                    title=(tool.title or tool.name)[:200],
                    description=tool.description[:1_000],
                    input_schema=tool.input_schema,
                    read_only=read_only,
                    destructive=tool.destructive_hint is not False,
                    idempotent=tool.idempotent_hint is True,
                )
            )
        return tuple(mapped)

    def catalog(self) -> tuple[McpToolDescriptor, ...]:
        return tuple(
            tool
            for state in self.states
            if state.available
            for tool in state.tools.values()
        )

    async def call(self, internal_name: str, arguments: dict[str, Any]) -> McpCallResult:
        """只读调用路径：写工具在此被硬拦截，只能走 call_write（行动计划）。"""
        descriptor = self._descriptor(internal_name)
        if not descriptor.read_only:
            raise McpManagerError("mcp_write_requires_action_plan")
        return await self._invoke(descriptor, arguments)

    async def call_write(self, internal_name: str, arguments: dict[str, Any]) -> McpCallResult:
        """写调用路径：仅供确认后的行动计划执行（MCP-D），聊天层不得触达。"""
        return await self._invoke(self._descriptor(internal_name), arguments)

    def _descriptor(self, internal_name: str) -> McpToolDescriptor:
        descriptor = next(
            (item for item in self.catalog() if item.internal_name == internal_name), None
        )
        if descriptor is None:
            raise McpManagerError("mcp_tool_unavailable")
        return descriptor

    async def _invoke(
        self, descriptor: McpToolDescriptor, arguments: dict[str, Any]
    ) -> McpCallResult:
        config = self._server_config(descriptor.server_id)
        try:
            async with self._client_factory(config) as client:
                payload = await client.call_tool(descriptor.remote_name, arguments)
        except Exception as error:
            return McpCallResult(
                ok=False,
                server_id=descriptor.server_id,
                tool_name=descriptor.internal_name,
                data=None,
                text="",
                reason_code=self._reason_for(error),
            )
        data, text = self._clip_result(
            payload.structured_content, payload.text, config.max_result_bytes
        )
        return McpCallResult(
            ok=not payload.is_error,
            server_id=descriptor.server_id,
            tool_name=descriptor.internal_name,
            data=data,
            text=text,
            reason_code="mcp_remote_tool_error" if payload.is_error else None,
        )

    @staticmethod
    def _clip_result(data: Any, text: str, limit: int) -> tuple[Any, str]:
        encoded = json.dumps(data, ensure_ascii=False, default=str).encode()
        clipped_data: Any = data
        if len(encoded) > limit:
            preview_limit = max(0, limit - 64)
            clipped_data = {
                "truncated": True,
                "preview": encoded[:preview_limit].decode("utf-8", "ignore"),
            }
        remaining = max(0, limit - len(json.dumps(clipped_data, ensure_ascii=False).encode()))
        clipped_text = text.encode()[:remaining].decode("utf-8", "ignore")
        return clipped_data, clipped_text

    @staticmethod
    def _reason_for(error: Exception) -> str:
        if isinstance(error, TimeoutError | asyncio.TimeoutError):
            return "mcp_timeout"
        return "mcp_connection_failed"
