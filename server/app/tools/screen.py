from __future__ import annotations

import base64
from dataclasses import dataclass
from time import perf_counter
from typing import Annotated, Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.devices import (
    CommandSnapshot,
    DeviceCommandConflict,
    DeviceCommandNotFound,
    DeviceSnapshot,
    DeviceTargetAmbiguous,
    DeviceTargetNotFound,
    DeviceTargetUnavailable,
    EphemeralDeviceAssetNotFound,
    EphemeralDeviceAssetStore,
)
from app.llm import ToolDefinition
from app.model_capabilities import CapabilityModelError, CapabilityModelService
from app.privacy import EgressBlocked
from app.schemas import PrivacyLevel

from .contracts import ToolContext, ToolResult


class CaptureScreenArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    target: Literal["main_display", "display", "active_window"] = "main_display"
    display_index: Annotated[int, Field(ge=1, le=32)] | None = None
    question: Annotated[str, Field(min_length=1, max_length=1_000)] = (
        "描述屏幕上与用户问题相关的可见内容；不要猜测屏幕外信息。"
    )

    @model_validator(mode="after")
    def validate_display_target(self) -> CaptureScreenArgs:
        if self.target == "display" and self.display_index is None:
            raise ValueError("display_index is required for display target")
        if self.target != "display" and self.display_index is not None:
            raise ValueError("display_index is only valid for display target")
        return self


@dataclass(frozen=True, slots=True)
class ScreenAnalysis:
    text: str
    provider: str


class ScreenAnalyzer(Protocol):
    async def analyze(
        self,
        *,
        data: bytes,
        media_type: str,
        prompt: str,
        privacy_level: PrivacyLevel,
    ) -> ScreenAnalysis: ...


class ScreenCommandGateway(Protocol):
    assets: EphemeralDeviceAssetStore

    async def issue(
        self,
        *,
        device_id: UUID,
        command: str,
        args: dict[str, JsonValue],
        idempotency_key: str,
        ttl_seconds: int,
    ) -> CommandSnapshot: ...

    async def wait_for_terminal(
        self,
        command_id: UUID,
        *,
        timeout_seconds: float | None = None,
    ) -> CommandSnapshot: ...


class ScreenTargetResolver(Protocol):
    async def resolve(
        self,
        *,
        owner_user_id: UUID,
        target: str | UUID | None,
        capability: str,
    ) -> DeviceSnapshot: ...


class CapabilityScreenAnalyzer:
    def __init__(self, service: CapabilityModelService) -> None:
        self._service = service

    async def analyze(
        self,
        *,
        data: bytes,
        media_type: str,
        prompt: str,
        privacy_level: PrivacyLevel,
    ) -> ScreenAnalysis:
        encoded = base64.b64encode(data).decode("ascii")
        result = await self._service.analyze_vision(
            prompt=prompt,
            image_urls=(f"data:{media_type};base64,{encoded}",),
            privacy_level=privacy_level,
            thinking=False,
            max_tokens=2_048,
        )
        return ScreenAnalysis(text=result.text, provider=result.model)


class CaptureScreenTool:
    name = "capture_screen"
    description = (
        "对已授权且在线的用户桌面设备执行一次活动窗口、主显示器或指定编号显示器截图，并使用已配置的视觉模型"
        "回答当前屏幕问题。device 可填设备 UUID、名称或别名；省略时仅在唯一候选时执行。"
    )
    arguments_model: type[BaseModel] = CaptureScreenArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(
        self,
        resolver: ScreenTargetResolver,
        gateway: ScreenCommandGateway,
        analyzer: ScreenAnalyzer,
    ) -> None:
        self._resolver = resolver
        self._gateway = gateway
        self._analyzer = analyzer

    def definition(self) -> ToolDefinition:
        return capture_screen_tool_definition()

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(CaptureScreenArgs, arguments)
        if PrivacyLevel(context.privacy_level) not in {PrivacyLevel.L1, PrivacyLevel.L2}:
            return self._failure("screen_capture_requires_l1", started)
        if context.user_id is None or context.turn_id is None:
            return self._failure("tool_context_missing", started)
        try:
            device = await self._resolver.resolve(
                owner_user_id=context.user_id,
                target=args.device,
                capability="screen.capture",
            )
        except DeviceTargetAmbiguous as error:
            return self._failure(
                "device_target_ambiguous",
                started,
                data={
                    "candidates": [
                        {
                            "device_id": str(item.device_id),
                            "name": item.name,
                            "alias": item.alias,
                        }
                        for item in error.candidates
                    ]
                },
            )
        except DeviceTargetUnavailable:
            return self._failure("device_unavailable", started)
        except DeviceTargetNotFound:
            return self._failure("device_target_not_found", started)
        try:
            command_args: dict[str, JsonValue] = {"target": args.target}
            if args.display_index is not None:
                command_args["display_index"] = args.display_index
            command = await self._gateway.issue(
                device_id=device.id,
                command="screen.capture",
                args=command_args,
                idempotency_key=f"screen-{context.turn_id}-{device.id}",
                ttl_seconds=30,
            )
        except (DeviceCommandConflict, DeviceCommandNotFound):
            return self._failure("device_command_rejected", started)
        if command.status not in {"succeeded", "failed", "cancelled", "expired", "timed_out"}:
            try:
                command = await self._gateway.wait_for_terminal(
                    command.id,
                    timeout_seconds=31,
                )
            except TimeoutError:
                return self._failure("device_command_timeout", started)
        if command.status != "succeeded" or command.result_meta is None:
            return self._failure(command.reason_code or f"command_{command.status}", started)
        try:
            asset_id = UUID(str(command.result_meta["asset_id"]))
            asset = await self._gateway.assets.consume(
                asset_id,
                owner_user_id=context.user_id,
                command_id=command.id,
            )
        except (EphemeralDeviceAssetNotFound, KeyError, ValueError):
            return self._failure("screen_asset_invalid", started)
        try:
            analysis = await self._analyzer.analyze(
                data=asset.data,
                media_type=asset.media_type,
                prompt=args.question,
                privacy_level=PrivacyLevel(context.privacy_level),
            )
        except CapabilityModelError as error:
            return self._failure(error.reason_code, started)
        except EgressBlocked:
            return self._failure("local_vision_required", started)
        except Exception:
            return self._failure("screen_analysis_failed", started)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider=analysis.provider,
            latency_ms=(perf_counter() - started) * 1_000,
            data={
                "device": device.alias or device.name,
                "target": args.target,
                "display_index": args.display_index,
                "analysis": analysis.text,
                "captured_at": asset.created_at.isoformat(),
            },
        )

    def _failure(
        self,
        reason_code: str,
        started: float,
        *,
        data: dict[str, object] | None = None,
    ) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason_code,
            latency_ms=(perf_counter() - started) * 1_000,
            data=data or {},
        )


def capture_screen_tool_definition() -> ToolDefinition:
    return ToolDefinition(
        name=CaptureScreenTool.name,
        description=CaptureScreenTool.description,
        parameters=CaptureScreenArgs.model_json_schema(),
    )
