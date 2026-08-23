from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.llm import EnvSecretProvider, ModelEndpoint, ModelKind
from app.privacy import EgressDestination, EgressGuard
from app.schemas import PrivacyLevel


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
    ) -> None:
        self._config_store = config_store
        self._secrets = secrets or EnvSecretProvider()
        self._egress = egress or EgressGuard()
        self._request_json = request_json

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
    ) -> VisionAnalysisResult:
        endpoint_name, endpoint = self._selected(ModelKind.VISION)
        self._authorize(endpoint_name, endpoint, privacy_level)
        families = int(bool(image_urls)) + int(video_url is not None) + int(bool(file_urls))
        if families != 1:
            raise CapabilityModelError("vision_requires_one_modality_family")

        content: list[dict[str, object]] = []
        if image_urls:
            content.extend(
                {"type": "image_url", "image_url": {"url": url}} for url in image_urls
            )
        elif video_url is not None:
            content.append({"type": "video_url", "video_url": {"url": video_url}})
        else:
            content.extend(
                {"type": "file_url", "file_url": {"url": url}} for url in file_urls
            )
        content.append({"type": "text", "text": prompt})

        request_body: dict[str, object] = {
            "model": endpoint.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
        }
        if endpoint.provider == "zhipu_native":
            request_body["thinking"] = {
                "type": "enabled" if thinking else "disabled"
            }

        started = perf_counter()
        payload = await self._post(
            endpoint,
            "/chat/completions",
            request_body,
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
        payload = await self._post(endpoint, "/images/generations", body)
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
        payload = await self._post(endpoint, "/videos/generations", body)
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
    ) -> AsyncGenerationResult:
        endpoint_name, endpoint = self._selected(ModelKind.VIDEO_GENERATION)
        self._authorize(endpoint_name, endpoint, privacy_level)
        payload = await self._get(endpoint, f"/async-result/{task_id}")
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
    ) -> object:
        return await asyncio.to_thread(
            self._request_json,
            "POST",
            self._url(endpoint, suffix),
            self._headers(endpoint),
            body,
            max(endpoint.timeout_ms / 1_000, 0.1),
        )

    async def _get(self, endpoint: ModelEndpoint, suffix: str) -> object:
        return await asyncio.to_thread(
            self._request_json,
            "GET",
            self._url(endpoint, suffix),
            self._headers(endpoint),
            None,
            max(endpoint.timeout_ms / 1_000, 0.1),
        )
