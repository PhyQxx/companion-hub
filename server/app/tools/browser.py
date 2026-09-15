from __future__ import annotations

import json
from time import perf_counter
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.devices import (
    DeviceCommandConflict,
    DeviceCommandNotFound,
    DeviceTargetAmbiguous,
    DeviceTargetNotFound,
    DeviceTargetUnavailable,
    EphemeralDeviceAssetNotFound,
)
from app.llm import ToolDefinition
from app.model_capabilities import CapabilityModelError
from app.privacy import EgressBlocked
from app.schemas import PrivacyLevel

from .contracts import ToolContext, ToolResult
from .screen import ScreenAnalyzer, ScreenCommandGateway, ScreenTargetResolver

MAX_TOOL_PAGE_TEXT = 30_000


class InspectWebpageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    mode: Literal["read", "capture"] = "read"
    question: Annotated[str, Field(min_length=1, max_length=1_000)] = (
        "概括当前网页与用户问题相关的可见内容，不要猜测页面外信息。"
    )


class BrowserDocumentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: Annotated[str, Field(max_length=500)]
    origin: Annotated[str, Field(max_length=2_048)]
    language: Annotated[str, Field(max_length=64)] | None = None
    text: Annotated[str, Field(max_length=100_000)]
    truncated: bool


class InspectWebpageTool:
    name = "inspect_webpage"
    description = (
        "读取已授权且在线的浏览器设备当前标签页。默认 mode=read 返回标题、网站 origin 和"
        "可见正文；只有问题依赖视觉布局或图片时才用 mode=capture 截图并交给本地视觉模型。"
    )
    arguments_model: type[BaseModel] = InspectWebpageArgs
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
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=InspectWebpageArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(InspectWebpageArgs, arguments)
        if PrivacyLevel(context.privacy_level) is PrivacyLevel.L0:
            # L0 公开模式不读取个人设备数据；L1/L2 会话均允许读取当前标签页。
            return self._failure("webpage_inspection_requires_l1", started)
        if context.user_id is None or context.turn_id is None:
            return self._failure("tool_context_missing", started)
        capability = (
            "browser.current_tab.read"
            if args.mode == "read"
            else "browser.current_tab.capture"
        )
        try:
            device = await self._resolver.resolve(
                owner_user_id=context.user_id,
                target=args.device,
                capability=capability,
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
        command_name = f"browser.current_tab.{args.mode}"
        try:
            command = await self._gateway.issue(
                device_id=device.id,
                command=command_name,
                args={},
                idempotency_key=(
                    f"browser-{args.mode}-{context.turn_id}-{device.id}"
                ),
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
            return self._failure("browser_asset_invalid", started)
        if args.mode == "read":
            try:
                payload = BrowserDocumentPayload.model_validate(json.loads(asset.data))
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
                return self._failure("browser_document_invalid", started)
            visible_text = payload.text[:MAX_TOOL_PAGE_TEXT]
            return ToolResult(
                ok=True,
                tool_name=self.name,
                provider="browser_bridge",
                latency_ms=(perf_counter() - started) * 1_000,
                data={
                    "device": device.alias or device.name,
                    "mode": args.mode,
                    "title": payload.title,
                    "origin": payload.origin,
                    "language": payload.language,
                    "visible_text": visible_text,
                    "truncated": payload.truncated or len(payload.text) > len(visible_text),
                },
            )
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
            return self._failure("webpage_analysis_failed", started)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider=analysis.provider,
            latency_ms=(perf_counter() - started) * 1_000,
            data={
                "device": device.alias or device.name,
                "mode": args.mode,
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
