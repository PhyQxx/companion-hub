from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import UUID

from app.config import ConfigSnapshot, ConfigStore, DatabaseConfigStore, HubConfig
from app.harness.budget import BudgetDenied, current_tool_budget
from app.harness.operations import CapabilityExecution, OperationPolicy
from app.llm import EnvSecretProvider, ModelEndpoint, ModelKind
from app.privacy import EgressDestination, EgressGuard
from app.runs.completion import current_model_owner
from app.schemas import PrivacyLevel

if TYPE_CHECKING:
    from app.db import Database


class CapabilityModelError(RuntimeError):
    def __init__(self, reason_code: str, *, detail: str | None = None) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class VisionAnalysisResult:
    text: str
    model: str
    request_id: str | None
    latency_ms: float


@dataclass(frozen=True, slots=True)
class ImageGenerationResult:
    urls: tuple[str, ...]
    model: str
    created: int | None
    latency_ms: float


@dataclass(frozen=True, slots=True)
class VideoGenerationTask:
    task_id: str
    request_id: str | None
    task_status: str
    model: str
    latency_ms: float


@dataclass(frozen=True, slots=True)
class AsyncGenerationResult:
    task_id: str
    task_status: str | None
    model: str | None
    video_urls: tuple[str, ...]
    cover_image_urls: tuple[str, ...]


class _ConfigSource(Protocol):
    @property
    def current(self) -> Any: ...


JsonRequester = Callable[
    [str, str, dict[str, str], dict[str, object] | None, float],
    object,
]


def _request_json(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, object] | None,
    timeout_seconds: float,
) -> object:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json", **headers}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(4_000_000)
    except HTTPError as error:
        raw = error.read(64_000)
        detail = raw.decode("utf-8", errors="replace")[:2_000]
        raise CapabilityModelError(
            "provider_http_error",
            detail=f"HTTP {error.code}: {detail}",
        ) from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CapabilityModelError("provider_invalid_json") from error


class CapabilityModelService:
    """Runtime access to non-text model capabilities selected in HubConfig.

    Text dialogue stays in LLMRouter. This service owns the capability-specific
    API shapes for vision understanding, image generation and video generation.
    """

    def __init__(
        self,
        config_store: ConfigStore | DatabaseConfigStore | _ConfigSource,
        *,
        secrets: EnvSecretProvider | None = None,
        egress: EgressGuard | None = None,
        request_json: JsonRequester = _request_json,
        database: Database | None = None,
        execution: CapabilityExecution | None = None,
    ) -> None:
        self._config_store = config_store
        self._secrets = secrets or EnvSecretProvider()
        self._egress = egress or EgressGuard()
        self._request_json = request_json
        if execution is None and database is not None:
            from app.runs.capability_sources import SqlCapabilityExecution

            execution = SqlCapabilityExecution(database)
        self._execution = execution

    @property
    def config(self) -> HubConfig:
        return self._config_store.current.config

    async def analyze_vision(
        self,
        *,
        prompt: str,
        image_urls: tuple[str, ...] = (),
        video_url: str | None = None,
        file_urls: tuple[str, ...] = (),
        privacy_level: PrivacyLevel = PrivacyLevel.L1,
        thinking: bool = True,
        max_tokens: int = 8_192,
        user_id: UUID | None = None,
    ) -> VisionAnalysisResult:
        endpoint_name, endpoint = self._selected(ModelKind.VISION)
        self._authorize(endpoint_name, endpoint, privacy_level)
        families = int(bool(image_urls)) + int(video_url is not None) + int(bool(file_urls))
        if families != 1:
            raise CapabilityModelError("vision_requires_one_modality_family")

        content: list[dict[str, object]] = []
        if image_urls:
            content.extend({"type": "image_url", "image_url": {"url": url}} for url in image_urls)
        elif video_url is not None:
            content.append({"type": "video_url", "video_url": {"url": video_url}})
        else:
            content.extend({"type": "file_url", "file_url": {"url": url}} for url in file_urls)
        content.append({"type": "text", "text": prompt})

        request_body: dict[str, object] = {
            "model": endpoint.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": min(max_tokens, endpoint.max_tokens or max_tokens),
        }
        if endpoint.provider == "zhipu_native":
            request_body["thinking"] = {"type": "enabled" if thinking else "disabled"}

        started = perf_counter()
        payload = await self._post(
            endpoint,
            "/chat/completions",
            request_body,
            endpoint_name=endpoint_name,
            privacy_level=privacy_level,
            user_id=user_id,
        )
        if not isinstance(payload, dict):
            raise CapabilityModelError("provider_invalid_response")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise CapabilityModelError("provider_missing_choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise CapabilityModelError("provider_missing_message")
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise CapabilityModelError("provider_empty_response")
        request_id = payload.get("id") or payload.get("request_id")
        return VisionAnalysisResult(
            text=text,
            model=endpoint.model,
            request_id=request_id if isinstance(request_id, str) else None,
            latency_ms=(perf_counter() - started) * 1_000,
        )

    async def generate_image(
        self,
        *,
        prompt: str,
        size: str = "1024x1024",
        quality: Literal["standard", "hd"] = "standard",
        privacy_level: PrivacyLevel = PrivacyLevel.L1,
        user_id: str | None = None,
    ) -> ImageGenerationResult:
        endpoint_name, endpoint = self._selected(ModelKind.IMAGE_GENERATION)
        self._authorize(endpoint_name, endpoint, privacy_level)
        body: dict[str, object] = {
            "model": endpoint.model,
            "prompt": prompt,
            "size": size,
            "quality": quality,
        }
        if user_id:
            body["user_id"] = user_id
        started = perf_counter()
        payload = await self._post(
            endpoint,
            "/images/generations",
            body,
            endpoint_name=endpoint_name,
            privacy_level=privacy_level,
            user_id=UUID(user_id) if self._execution is not None and user_id else None,
        )
        if not isinstance(payload, dict):
            raise CapabilityModelError("provider_invalid_response")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise CapabilityModelError("provider_missing_image_data")
        urls = tuple(
            value
            for row in rows
            if isinstance(row, dict)
            for value in [row.get("url")]
            if isinstance(value, str) and value
        )
        if not urls:
            raise CapabilityModelError("provider_empty_image_result")
        created = payload.get("created")
        return ImageGenerationResult(
            urls=urls,
            model=endpoint.model,
            created=created if isinstance(created, int) else None,
            latency_ms=(perf_counter() - started) * 1_000,
        )

    async def generate_video(
        self,
        *,
        prompt: str | None = None,
        image_url: str | tuple[str, str] | None = None,
        quality: Literal["speed", "quality"] = "speed",
        size: str = "1280x720",
        fps: Literal[30, 60] = 30,
        duration: Literal[5, 10] = 5,
        with_audio: bool = False,
        privacy_level: PrivacyLevel = PrivacyLevel.L1,
        user_id: str | None = None,
    ) -> VideoGenerationTask:
        endpoint_name, endpoint = self._selected(ModelKind.VIDEO_GENERATION)
        self._authorize(endpoint_name, endpoint, privacy_level)
        if not prompt and image_url is None:
            raise CapabilityModelError("video_requires_prompt_or_image")
        body: dict[str, object] = {
            "model": endpoint.model,
            "quality": quality,
            "size": size,
            "fps": fps,
            "duration": duration,
            "with_audio": with_audio,
        }
        if prompt:
            body["prompt"] = prompt
        if image_url is not None:
            body["image_url"] = list(image_url) if isinstance(image_url, tuple) else image_url
        if user_id:
            body["user_id"] = user_id
        started = perf_counter()
        payload = await self._post(
            endpoint,
            "/videos/generations",
            body,
            endpoint_name=endpoint_name,
            privacy_level=privacy_level,
            user_id=UUID(user_id) if self._execution is not None and user_id else None,
        )
        if not isinstance(payload, dict):
            raise CapabilityModelError("provider_invalid_response")
        task_id = payload.get("id")
        task_status = payload.get("task_status")
        if not isinstance(task_id, str) or not task_id:
            raise CapabilityModelError("provider_missing_task_id")
        request_id = payload.get("request_id")
        return VideoGenerationTask(
            task_id=task_id,
            request_id=request_id if isinstance(request_id, str) else None,
            task_status=task_status if isinstance(task_status, str) else "PROCESSING",
            model=endpoint.model,
            latency_ms=(perf_counter() - started) * 1_000,
        )

    async def async_result(
        self,
        task_id: str,
        *,
        privacy_level: PrivacyLevel = PrivacyLevel.L1,
        user_id: UUID | None = None,
    ) -> AsyncGenerationResult:
        ticket_guard: Callable[[], Awaitable[None]] | None = None
        endpoint_name, endpoint = self._selected(ModelKind.VIDEO_GENERATION)
        if self._execution is not None:
            owner = user_id or current_model_owner()
            if owner is None:
                raise BudgetDenied("model_run_owner_missing")
            execution = self._execution
            ticket = await execution.ticket(owner, task_id)
            if ticket is None:
                raise CapabilityModelError("generation_task_not_found")
            if ticket.endpoint_fingerprint != _endpoint_fingerprint(
                endpoint_name, endpoint, self._headers(endpoint)
            ):
                raise CapabilityModelError("generation_task_endpoint_changed")
            privacy_level = ticket.privacy_level

            async def verify_ticket() -> None:
                await execution.validate_ticket(ticket)

            ticket_guard = verify_ticket
        self._authorize(endpoint_name, endpoint, privacy_level)
        payload = await self._request_with_retries(
            endpoint,
            "GET",
            f"/async-result/{quote(task_id, safe='')}",
            None,
            endpoint_name=endpoint_name,
            privacy_level=privacy_level,
            user_id=user_id,
            source_guard=ticket_guard,
        )
        if not isinstance(payload, dict):
            raise CapabilityModelError("provider_invalid_response")
        rows = payload.get("video_result")
        video_urls: list[str] = []
        cover_urls: list[str] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                video_url = row.get("url")
                cover_url = row.get("cover_image_url")
                if isinstance(video_url, str) and video_url:
                    video_urls.append(video_url)
                if isinstance(cover_url, str) and cover_url:
                    cover_urls.append(cover_url)
        status = payload.get("task_status")
        model = payload.get("model")
        return AsyncGenerationResult(
            task_id=task_id,
            task_status=status if isinstance(status, str) else None,
            model=model if isinstance(model, str) else None,
            video_urls=tuple(video_urls),
            cover_image_urls=tuple(cover_urls),
        )

    def _selected(self, kind: ModelKind) -> tuple[str, ModelEndpoint]:
        config = self.config
        field_name = {
            ModelKind.VISION: "vision",
            ModelKind.IMAGE_GENERATION: "image_generation",
            ModelKind.VIDEO_GENERATION: "video_generation",
        }.get(kind)
        if field_name is None:
            raise CapabilityModelError("unsupported_capability_kind")
        endpoint_name = getattr(config.capability_models, field_name)
        if endpoint_name is None:
            raise CapabilityModelError(f"{field_name}_model_not_configured")
        endpoint = config.models[endpoint_name]
        if ModelKind(endpoint.kind) is not kind:
            raise CapabilityModelError("capability_model_kind_mismatch")
        if endpoint.provider not in {"openai_compatible", "zhipu_native"}:
            raise CapabilityModelError("unsupported_capability_provider")
        return endpoint_name, endpoint

    def _authorize(
        self,
        endpoint_name: str,
        endpoint: ModelEndpoint,
        privacy_level: PrivacyLevel,
    ) -> None:
        self._egress.authorize(
            PrivacyLevel(privacy_level),
            EgressDestination(
                name=endpoint_name,
                runs_local=endpoint.runs_local,
                max_privacy_level=PrivacyLevel(endpoint.max_privacy_level),
            ),
        )

    def _headers(self, endpoint: ModelEndpoint) -> dict[str, str]:
        secret = endpoint.secret_value
        if secret is None and endpoint.secret_ref is not None:
            secret = self._secrets.resolve(endpoint.secret_ref)
        if not secret and endpoint.runs_local:
            return {}
        if not secret:
            raise CapabilityModelError("model_secret_unavailable")
        return {"Authorization": f"Bearer {secret}"}

    def _url(self, endpoint: ModelEndpoint, suffix: str) -> str:
        return f"{str(endpoint.base_url).rstrip('/')}{suffix}"

    async def _post(
        self,
        endpoint: ModelEndpoint,
        suffix: str,
        body: dict[str, object],
        *,
        endpoint_name: str,
        privacy_level: PrivacyLevel,
        user_id: UUID | None,
    ) -> object:
        return await self._request_with_retries(
            endpoint,
            "POST",
            suffix,
            body,
            endpoint_name=endpoint_name,
            privacy_level=privacy_level,
            user_id=user_id,
        )

    async def _request_with_retries(
        self,
        endpoint: ModelEndpoint,
        method: str,
        suffix: str,
        body: dict[str, object] | None,
        *,
        endpoint_name: str,
        privacy_level: PrivacyLevel,
        user_id: UUID | None,
        source_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> object:
        entry = {
            "/videos/generations": "capability.video.submit",
            "/images/generations": "capability.image.generate",
            "/chat/completions": "capability.vision.analyze",
        }.get(suffix, "capability.video.query")
        owner = user_id or current_model_owner()
        fingerprint = _endpoint_fingerprint(endpoint_name, endpoint, self._headers(endpoint))

        async def check() -> None:
            if source_guard is not None:
                await source_guard()
            if self._selected(ModelKind(endpoint.kind))[0] != endpoint_name:
                raise BudgetDenied("capability_endpoint_changed")
            current = self.config.models.get(endpoint_name)
            if (
                current is None
                or not current.enabled
                or _endpoint_fingerprint(endpoint_name, current, self._headers(current))
                != fingerprint
            ):
                raise BudgetDenied("capability_endpoint_changed")
            self._authorize(endpoint_name, current, privacy_level)

        async def invoke(mark_started: Callable[[], Awaitable[None]] | None = None) -> object:
            # A failed generation POST may already be accepted/billed remotely.
            retries = (
                0 if self._execution is not None and method == "POST" else endpoint.max_retries
            )
            for attempt in range(retries + 1):
                await check()
                port = current_tool_budget() if self._execution is not None else None
                permit = await port.reserve_tool(tool_name=entry, user_id=owner) if port else None
                returned = False
                dispatched = False
                try:
                    if mark_started is not None:
                        await mark_started()
                    dispatched = True
                    async with asyncio.timeout(
                        permit.remaining_seconds if permit else endpoint.timeout_ms / 1_000
                    ):
                        result = await asyncio.to_thread(
                            self._request_json,
                            method,
                            self._url(endpoint, suffix),
                            self._headers(endpoint),
                            body,
                            max(endpoint.timeout_ms / 1_000, 0.1),
                        )
                    returned = True
                    return result
                except CapabilityModelError as error:
                    if attempt >= retries or not _retryable_provider_error(error):
                        raise
                finally:
                    if port is not None and permit is not None:
                        await port.settle_tool(
                            permit.call_id,
                            reported_ok=True if returned else None if dispatched else False,
                        )
                await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
            raise AssertionError("capability model retry loop exhausted")

        if self._execution is None:
            return await invoke()
        if owner is None:
            raise BudgetDenied("model_run_owner_missing")

        def evidence(result: object) -> dict[str, str]:
            fields = {"endpoint_fingerprint": fingerprint}
            if isinstance(result, dict):
                receipt = result.get("request_id") or result.get("id")
                if isinstance(receipt, str) and receipt and len(receipt) <= 200:
                    fields["provider_request_id"] = receipt
            if entry == "capability.video.submit" and isinstance(result, dict):
                identifier = result.get("id")
                if isinstance(identifier, str) and identifier and len(identifier) <= 256:
                    fields["provider_task_id"] = identifier
            return fields

        snapshot: ConfigSnapshot = self._config_store.current
        return await self._execution.execute(
            OperationPolicy(
                snapshot.version, tuple(snapshot.config.run_budget.model_dump(mode="json").items())
            ),
            user_id=owner,
            privacy_level=privacy_level,
            entry=entry,
            invoke=invoke,
            evidence=evidence,
            source_guard=check,
            cost_endpoint=endpoint_name,
        )


def _endpoint_fingerprint(name: str, endpoint: ModelEndpoint, credentials: dict[str, str]) -> str:
    data = {
        "name": name,
        "credentials": credentials,
        "kind": str(endpoint.kind),
        "provider": endpoint.provider,
        "model": endpoint.model,
        "base_url": str(endpoint.base_url),
        "runs_local": endpoint.runs_local,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _retryable_provider_error(error: CapabilityModelError) -> bool:
    if error.reason_code != "provider_http_error" or error.detail is None:
        return False
    status_text = error.detail.removeprefix("HTTP ").split(":", 1)[0]
    try:
        status_code = int(status_text)
    except ValueError:
        return False
    return status_code == 429 or 500 <= status_code < 600
