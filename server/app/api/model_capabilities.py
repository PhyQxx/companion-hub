from typing import Annotated, Literal, NoReturn, Self

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AnyHttpUrl, Field, model_validator

from app.auth import AuthService, ChatPrincipal
from app.model_capabilities import CapabilityModelError, CapabilityModelService
from app.privacy import EgressBlocked
from app.schemas import PrivacyLevel
from app.schemas.common import StrictModel

from .auth import ChatSessionGuard


class VisionAnalyzeRequest(StrictModel):
    prompt: Annotated[str, Field(min_length=1, max_length=20_000)]
    image_urls: Annotated[list[AnyHttpUrl], Field(max_length=16)] = Field(default_factory=list)
    video_url: AnyHttpUrl | None = None
    file_urls: Annotated[list[AnyHttpUrl], Field(max_length=8)] = Field(default_factory=list)
    thinking: bool = True
    max_tokens: Annotated[int, Field(ge=1, le=65_536)] = 8_192
    privacy_level: Literal["L0", "L1", "L2"] = "L1"

    @model_validator(mode="after")
    def exactly_one_modality_family(self) -> Self:
        families = int(bool(self.image_urls)) + int(self.video_url is not None) + int(
            bool(self.file_urls)
        )
        if families != 1:
            raise ValueError("exactly one of image_urls, video_url or file_urls is required")
        return self


class VisionAnalyzeResponse(StrictModel):
    text: str
    model: str
    request_id: str | None
    latency_ms: float


class ImageGenerateRequest(StrictModel):
    prompt: Annotated[str, Field(min_length=1, max_length=10_000)]
    size: Literal[
        "1024x1024",
        "768x1344",
        "864x1152",
        "1344x768",
        "1152x864",
        "1440x720",
        "720x1440",
    ] = "1024x1024"
    quality: Literal["standard", "hd"] = "standard"
    privacy_level: Literal["L0", "L1", "L2"] = "L1"


class ImageGenerateResponse(StrictModel):
    urls: list[str]
    model: str
    created: int | None
    latency_ms: float


class VideoGenerateRequest(StrictModel):
    prompt: Annotated[str, Field(min_length=1, max_length=10_000)] | None = None
    image_url: AnyHttpUrl | None = None
    end_image_url: AnyHttpUrl | None = None
    quality: Literal["speed", "quality"] = "speed"
    size: Literal[
        "1280x720",
        "720x1280",
        "1024x1024",
        "1920x1080",
        "1080x1920",
        "2048x1080",
        "3840x2160",
    ] = "1280x720"
    fps: Literal[30, 60] = 30
    duration: Literal[5, 10] = 5
    with_audio: bool = False
    privacy_level: Literal["L0", "L1", "L2"] = "L1"

    @model_validator(mode="after")
    def prompt_or_image(self) -> Self:
        if self.prompt is None and self.image_url is None:
            raise ValueError("prompt or image_url is required")
        if self.end_image_url is not None and self.image_url is None:
            raise ValueError("end_image_url requires image_url")
        return self


class VideoGenerateResponse(StrictModel):
    task_id: str
    request_id: str | None
    task_status: str
    model: str
    latency_ms: float


class GenerationTaskResponse(StrictModel):
    task_id: str
    task_status: str | None
    model: str | None
    video_urls: list[str]
    cover_image_urls: list[str]


def _raise_capability_http(error: Exception) -> NoReturn:
    if isinstance(error, EgressBlocked):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="privacy level is not allowed for the selected cloud capability model",
        ) from error
    if isinstance(error, CapabilityModelError):
        code = (
            status.HTTP_409_CONFLICT
            if error.reason_code.endswith("_not_configured")
            or error.reason_code in {
                "model_secret_unavailable",
                "unsupported_capability_provider",
            }
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(code, detail=error.reason_code) from error
    raise error


def create_model_capability_router(
    service: CapabilityModelService,
    auth_service: AuthService,
) -> APIRouter:
    chat_guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/model-capabilities", tags=["model-capabilities"])

    @router.post("/vision/analyze", response_model=VisionAnalyzeResponse)
    async def analyze_vision(
        body: VisionAnalyzeRequest,
        principal: Annotated[ChatPrincipal, Depends(chat_guard)],
    ) -> VisionAnalyzeResponse:
        del principal
        try:
            result = await service.analyze_vision(
                prompt=body.prompt,
                image_urls=tuple(str(url) for url in body.image_urls),
                video_url=str(body.video_url) if body.video_url is not None else None,
                file_urls=tuple(str(url) for url in body.file_urls),
                privacy_level=PrivacyLevel(body.privacy_level),
                thinking=body.thinking,
                max_tokens=body.max_tokens,
            )
        except Exception as error:
            _raise_capability_http(error)
        return VisionAnalyzeResponse(
            text=result.text,
            model=result.model,
            request_id=result.request_id,
            latency_ms=result.latency_ms,
        )

    @router.post("/images/generate", response_model=ImageGenerateResponse)
    async def generate_image(
        body: ImageGenerateRequest,
        principal: Annotated[ChatPrincipal, Depends(chat_guard)],
    ) -> ImageGenerateResponse:
        try:
            result = await service.generate_image(
                prompt=body.prompt,
                size=body.size,
                quality=body.quality,
                privacy_level=PrivacyLevel(body.privacy_level),
                user_id=str(principal.user_id),
            )
        except Exception as error:
            _raise_capability_http(error)
        return ImageGenerateResponse(
            urls=list(result.urls),
            model=result.model,
            created=result.created,
            latency_ms=result.latency_ms,
        )

    @router.post("/videos/generate", response_model=VideoGenerateResponse)
    async def generate_video(
        body: VideoGenerateRequest,
        principal: Annotated[ChatPrincipal, Depends(chat_guard)],
    ) -> VideoGenerateResponse:
        image_input: str | tuple[str, str] | None = None
        if body.image_url is not None and body.end_image_url is not None:
            image_input = (str(body.image_url), str(body.end_image_url))
        elif body.image_url is not None:
            image_input = str(body.image_url)
        try:
            result = await service.generate_video(
                prompt=body.prompt,
                image_url=image_input,
                quality=body.quality,
                size=body.size,
                fps=body.fps,
                duration=body.duration,
                with_audio=body.with_audio,
                privacy_level=PrivacyLevel(body.privacy_level),
                user_id=str(principal.user_id),
            )
        except Exception as error:
            _raise_capability_http(error)
        return VideoGenerateResponse(
            task_id=result.task_id,
            request_id=result.request_id,
            task_status=result.task_status,
            model=result.model,
            latency_ms=result.latency_ms,
        )

    @router.get("/tasks/{task_id}", response_model=GenerationTaskResponse)
    async def generation_task(
        task_id: str,
        principal: Annotated[ChatPrincipal, Depends(chat_guard)],
    ) -> GenerationTaskResponse:
        del principal
        try:
            result = await service.async_result(task_id)
        except Exception as error:
            _raise_capability_http(error)
        return GenerationTaskResponse(
            task_id=result.task_id,
            task_status=result.task_status,
            model=result.model,
            video_urls=list(result.video_urls),
            cover_image_urls=list(result.cover_image_urls),
        )

    return router
