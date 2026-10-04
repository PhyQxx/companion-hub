from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import McpServerConfig
from app.harness.guarded_call import guarded_inline_call
from app.harness.joined_read import join_on_cancel

from .client import SdkMcpClient, sdk_client_factory
from .models import McpCallPayload, McpCallResult, McpRemoteTool, McpServerState, McpToolDescriptor
from .ports import McpClientFactory, McpRemoteClient

logger = logging.getLogger("app.integrations.mcp")

MAX_CATALOG_PAGES = 100
T = TypeVar("T")


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
        self._active: set[asyncio.Task[Any]] = set()
        self._closing: set[asyncio.Task[Any]] = set()
        self._epoch = 0
        # Connection secrets remain private in memory, never in public states.
        self._catalog_sources: dict[str, tuple[int | None, McpServerConfig, str | None]] = {}
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
        return tuple(deepcopy(self._states[key]) for key in sorted(self._states))

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
        self._epoch += 1
        task, self._task = self._task, None
        pending = self._active | ({task} if task is not None else set())
        pending.discard(asyncio.current_task())
        for operation in pending:
            if not operation.done() and operation not in self._closing:
                operation.cancel()
        self._sync_configured_states()
        if pending:

            async def finish() -> None:
                await asyncio.gather(*pending, return_exceptions=True)

            await join_on_cancel(finish(), name="mcp-stop-cleanup")

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
        changed = False
        for server_id, item in configured.items():
            state = self._states.setdefault(server_id, McpServerState(server_id=server_id))
            state.configured_enabled = config.enabled and item.enabled
            accepted = self._catalog_sources.get(server_id)
            stale = accepted is not None and accepted != self._connection_source(item)
            if not state.configured_enabled or stale or self._stop.is_set():
                changed = changed or bool(state.tools)
                self._catalog_sources.pop(server_id, None)
                state.available = False
                state.tools.clear()
                state.tool_count = 0
                state.protocol_version = state.server_name = state.server_version = None
                state.last_refresh_at = None
                if stale:
                    state.last_error = "mcp_source_changed"
        for server_id in set(self._states) - set(configured):
            changed = changed or bool(self._states[server_id].tools)
            del self._states[server_id]
            self._catalog_sources.pop(server_id, None)
        if changed:
            self._notify_catalog_changed()

    def _connection_source(
        self, config: McpServerConfig
    ) -> tuple[int | None, McpServerConfig, str | None]:
        secret = config.secret_value
        if secret is None and config.secret_ref is not None:
            secret = os.environ.get(config.secret_ref.removeprefix("env:"))
        return getattr(self._config_store.current, "version", None), deepcopy(config), secret

    def _check_source(
        self, server_id: str, source: tuple[int | None, McpServerConfig, str | None], epoch: int
    ) -> None:
        if self._stop.is_set() or self._epoch != epoch:
            raise McpManagerError("mcp_manager_stopped")
        try:
            current = self._server_config(server_id)
        except McpManagerError:
            raise McpManagerError("mcp_source_changed") from None
        if not self.enabled or not current.enabled or source != self._connection_source(current):
            raise McpManagerError("mcp_source_changed")
        if source[1].secret_ref is not None and not source[2]:
            raise McpManagerError("mcp_secret_unavailable")

    async def _connection_call(
        self,
        server_id: str,
        source: tuple[int | None, McpServerConfig, str | None],
        invoke: Callable[[], Awaitable[T]],
        *,
        epoch: int,
        capability_guard: Callable[[], None] | None = None,
    ) -> T:
        owner = asyncio.current_task()
        assert owner is not None

        async def check() -> None:
            # Disconnect keeps the SDK context on its owning task. The caller
            # checks authority again after disconnect, before publishing.
            if owner not in self._closing:
                self._check_source(server_id, source, epoch)
                if capability_guard is not None:
                    capability_guard()

        self._active.add(owner)
        try:
            try:
                return await guarded_inline_call(invoke, check)
            except Exception:
                # Failed connection acquisition may have spent time closing.
                # Revalidate that terminal window too, without starting again.
                self._check_source(server_id, source, epoch)
                if capability_guard is not None:
                    capability_guard()
                raise
        finally:
            self._active.discard(owner)
            self._sync_configured_states()

    @contextlib.asynccontextmanager
    async def _client_scope(self, config: McpServerConfig) -> AsyncIterator[McpRemoteClient]:
        owner = asyncio.current_task()
        assert owner is not None
        remote: McpRemoteClient | None = None
        try:
            remote = self._client_factory(config)
            if isinstance(remote, SdkMcpClient):
                remote.set_close_observer(lambda: self._closing.add(owner))
            async with remote as client:
                try:
                    yield client
                finally:
                    self._closing.add(owner)
        finally:
            self._closing.discard(owner)
            if isinstance(remote, SdkMcpClient):
                remote.set_close_observer(None)

    async def refresh_expired(self) -> None:
        self._sync_configured_states()
        now = self._clock()
        for state in self.states:
            if not state.configured_enabled:
                continue
            config = self._server_config(state.server_id)
            if state.last_refresh_at is None or now - state.last_refresh_at >= timedelta(
                seconds=config.catalog_ttl_seconds
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
        source = self._connection_source(config)
        epoch = self._epoch
        async with lock:
            # Waiting for another refresh cannot replace the accepted authority.
            self._check_source(server_id, source, epoch)
            state.refreshing = True
            try:

                async def fetch() -> tuple[
                    tuple[McpToolDescriptor, ...], str | None, str | None, str | None
                ]:
                    remote_tools: list[McpRemoteTool] = []
                    cursor: str | None = None
                    pinned = config.model_copy(
                        update={"secret_value": source[2], "secret_ref": None}
                    )
                    async with self._client_scope(pinned) as client:
                        for _ in range(MAX_CATALOG_PAGES):
                            self._check_source(server_id, source, epoch)
                            page, cursor = await client.list_tools(cursor)
                            remote_tools.extend(deepcopy(page))
                            if cursor is None:
                                break
                        else:
                            raise McpManagerError("mcp_catalog_page_limit")
                        candidate = (
                            self._map_tools(config, remote_tools),
                            client.protocol_version,
                            client.server_name,
                            client.server_version,
                        )
                    return candidate

                tools, protocol, name, version = await self._connection_call(
                    server_id, source, fetch, epoch=epoch
                )
                self._check_source(server_id, source, epoch)
                previous_source = self._catalog_sources.get(server_id)
                issued: list[McpToolDescriptor] = []
                for descriptor in tools:
                    previous = state.tools.get(descriptor.internal_name)
                    ticket = (
                        previous.catalogue_ticket
                        if previous_source == source
                        and previous is not None
                        and replace(previous, catalogue_ticket=None) == descriptor
                        else None
                    )
                    issued.append(
                        replace(descriptor, catalogue_ticket=ticket or secrets.token_hex(32))
                    )
                tools = tuple(issued)
                self._catalog_sources[server_id] = source
                state.protocol_version, state.server_name, state.server_version = (
                    protocol,
                    name,
                    version,
                )
                state.tools = {item.internal_name: item for item in tools}
                state.tool_count = len(tools)
                state.available = True
                state.last_refresh_at = self._clock()
                state.last_error = None
                state.consecutive_failures = 0
            except McpManagerError as error:
                had_tools = bool(state.tools)
                self._catalog_sources.pop(server_id, None)
                state.available = False
                state.tools.clear()
                state.tool_count = 0
                state.consecutive_failures += 1
                state.last_error = error.reason_code
                if had_tools:
                    self._notify_catalog_changed()
                raise
            except Exception as error:
                had_tools = bool(state.tools)
                self._catalog_sources.pop(server_id, None)
                state.available = False
                state.tools.clear()
                state.tool_count = 0
                state.consecutive_failures += 1
                state.last_error = self._reason_for(error)
                if had_tools:
                    self._notify_catalog_changed()
                logger.warning(
                    "MCP catalog refresh failed server_id=%s error_type=%s",
                    server_id,
                    type(error).__name__,
                )
            finally:
                state.refreshing = False
        if state.available:
            self._notify_catalog_changed()
        return deepcopy(state)

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
            deepcopy(tool)
            for state in self.states
            if state.available
            for tool in state.tools.values()
        )

    async def call(
        self, internal_name: str, arguments: dict[str, Any], *, catalogue_ticket: str | None = None
    ) -> McpCallResult:
        """只读调用路径：写工具在此被硬拦截，只能走 call_write（行动计划）。"""
        descriptor = self._descriptor(internal_name)
        self._check_ticket(descriptor, catalogue_ticket)
        if not descriptor.read_only:
            raise McpManagerError("mcp_write_requires_action_plan")
        return await self._invoke(descriptor, arguments)

    async def call_write(
        self, internal_name: str, arguments: dict[str, Any], *, catalogue_ticket: str | None = None
    ) -> McpCallResult:
        """写调用路径：仅供确认后的行动计划执行（MCP-D），聊天层不得触达。"""
        descriptor = self._descriptor(internal_name)
        self._check_ticket(descriptor, catalogue_ticket)
        return await self._invoke(descriptor, arguments)

    @staticmethod
    def _check_ticket(descriptor: McpToolDescriptor, ticket: str | None) -> None:
        if ticket is not None and ticket != descriptor.catalogue_ticket:
            raise McpManagerError("mcp_tool_source_changed")

    def validate_catalogue_ticket(
        self, internal_name: str, ticket: str | None, *, write: bool = False
    ) -> None:
        if ticket is None:
            raise McpManagerError("mcp_tool_source_missing")
        descriptor = self._descriptor(internal_name)
        self._check_ticket(descriptor, ticket)
        if not write and not descriptor.read_only:
            raise McpManagerError("mcp_write_requires_action_plan")

    def _check_tool_source(self, descriptor: McpToolDescriptor) -> None:
        state = self._states.get(descriptor.server_id)
        if (
            state is None
            or not state.available
            or state.tools.get(descriptor.internal_name) != descriptor
        ):
            raise McpManagerError("mcp_tool_source_changed")

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
        source = self._catalog_sources.get(descriptor.server_id)
        if source is None:
            raise McpManagerError("mcp_tool_unavailable")
        epoch = self._epoch
        try:

            async def invoke() -> McpCallPayload:
                pinned = config.model_copy(update={"secret_value": source[2], "secret_ref": None})
                async with self._client_scope(pinned) as client:
                    self._check_source(descriptor.server_id, source, epoch)
                    self._check_tool_source(descriptor)
                    result = deepcopy(
                        await client.call_tool(descriptor.remote_name, deepcopy(arguments))
                    )
                return result

            payload = await self._connection_call(
                descriptor.server_id,
                source,
                invoke,
                epoch=epoch,
                capability_guard=lambda: self._check_tool_source(descriptor),
            )
            self._check_source(descriptor.server_id, source, epoch)
            self._check_tool_source(descriptor)
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
        if isinstance(error, McpManagerError):
            return error.reason_code
        if isinstance(error, TimeoutError | asyncio.TimeoutError):
            return "mcp_timeout"
        return "mcp_connection_failed"
