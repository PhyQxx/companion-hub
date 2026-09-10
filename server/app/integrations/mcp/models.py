from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class McpRemoteTool:
    name: str
    title: str | None
    description: str
    input_schema: dict[str, Any]
    read_only_hint: bool | None
    destructive_hint: bool | None
    idempotent_hint: bool | None


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    internal_name: str
    server_id: str
    remote_name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    destructive: bool
    idempotent: bool


@dataclass(frozen=True, slots=True)
class McpCallPayload:
    is_error: bool
    structured_content: Any
    text: str


@dataclass(frozen=True, slots=True)
class McpCallResult:
    ok: bool
    server_id: str
    tool_name: str
    data: Any
    text: str
    reason_code: str | None = None


@dataclass(slots=True)
class McpServerState:
    server_id: str
    configured_enabled: bool = False
    available: bool = False
    refreshing: bool = False
    protocol_version: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    tool_count: int = 0
    last_refresh_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    tools: dict[str, McpToolDescriptor] = field(default_factory=dict)
