from __future__ import annotations

from collections.abc import Awaitable
from time import perf_counter
from typing import Annotated, Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.llm import ToolDefinition
from app.schemas import PrivacyLevel

from .contracts import ToolContext, ToolResult


class ResolvedDesktopDevice(Protocol):
    @property
    def id(self) -> UUID: ...


class DesktopTargetResolver(Protocol):
    def resolve(
        self,
        *,
        owner_user_id: UUID,
        target: str | UUID | None,
        capability: str,
    ) -> Awaitable[ResolvedDesktopDevice]: ...


class DesktopCommand(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def status(self) -> str: ...

    @property
    def reason_code(self) -> str | None: ...


class DesktopCommandGateway(Protocol):
    def issue(
        self,
        *,
        device_id: UUID,
        command: str,
        args: dict[str, JsonValue],
        idempotency_key: str,
        ttl_seconds: int,
    ) -> Awaitable[DesktopCommand]: ...

    def wait_for_terminal(
        self,
        command_id: UUID,
        *,
        timeout_seconds: float | None = None,
    ) -> Awaitable[DesktopCommand]: ...


class DesktopNotifyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: Annotated[str, Field(min_length=1, max_length=80)]
    body: Annotated[str, Field(min_length=1, max_length=500)]
    privacy_level: Literal["L0", "L1"]
    target: Annotated[str, Field(min_length=1, max_length=160)] | None = None


class DesktopNotifyTool:
    name = "desktop_notify"
    description = (
        "向用户已授权且在线的桌面设备发送一条 L0/L1 系统通知。"
        "不接受 L2 私密正文，设备必须声明 notification.show 能力。"
    )
    arguments_model: type[BaseModel] = DesktopNotifyArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L1

    def __init__(
        self,
        resolver: DesktopTargetResolver,
        gateway: DesktopCommandGateway,
    ) -> None:
        self._resolver = resolver
        self._gateway = gateway

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=DesktopNotifyArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(DesktopNotifyArgs, arguments)
        if context.user_id is None:
            return _failure(self.name, "action_user_required", started)
        if context.idempotency_key is None:
            return _failure(self.name, "action_idempotency_required", started)
        if str(context.privacy_level) != args.privacy_level:
            return _failure(self.name, "notification_privacy_context_mismatch", started)
        try:
            device = await self._resolver.resolve(
                owner_user_id=context.user_id,
                target=args.target,
                capability="notification.show",
            )
        except RuntimeError:
            return _failure(self.name, "desktop_channel_unavailable", started)
        command = await self._gateway.issue(
            device_id=device.id,
            command="notification.show",
            args={
                "title": args.title,
                "body": args.body,
                "privacy_level": args.privacy_level,
            },
            idempotency_key=context.idempotency_key,
            ttl_seconds=8,
        )
        terminal = await self._gateway.wait_for_terminal(command.id, timeout_seconds=9)
        ok = terminal.status == "succeeded"
        data: dict[str, JsonValue] = {
            "command_id": str(command.id),
            "device_id": str(device.id),
            "status": terminal.status,
        }
        return ToolResult(
            ok=ok,
            tool_name=self.name,
            provider="device_command",
            data=data,
            reason_code=None if ok else terminal.reason_code or "device_command_failed",
            latency_ms=(perf_counter() - started) * 1_000,
        )


def _failure(tool_name: str, reason_code: str, started: float) -> ToolResult:
    return ToolResult(
        ok=False,
        tool_name=tool_name,
        reason_code=reason_code,
        latency_ms=(perf_counter() - started) * 1_000,
    )
