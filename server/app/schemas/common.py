from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.types import UuidVersion

UUID7: TypeAlias = Annotated[UUID, UuidVersion(7)]
NamespacedName: TypeAlias = Annotated[
    str,
    Field(min_length=3, max_length=160, pattern=r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)+$"),
]
TokenName: TypeAlias = Annotated[
    str,
    Field(min_length=1, max_length=160, pattern=r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)*$"),
]
SchemaRef: TypeAlias = Annotated[
    str,
    Field(min_length=3, max_length=200, pattern=r"^[a-z][a-z0-9_.-]*/[1-9]\d*$"),
]
Sha256: TypeAlias = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
JsonScalar: TypeAlias = str | int | float | bool | None
JsonArgs: TypeAlias = dict[str, JsonScalar | list[JsonScalar]]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class PrivacyLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class SourceRef(StrictModel):
    adapter_id: NamespacedName
    adapter_instance_id: UUID7
    endpoint_id: Annotated[str, Field(min_length=1, max_length=160)]


class AssetPointer(StrictModel):
    asset_id: UUID7
    media_type: Annotated[str, Field(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")]
    content_hash: Sha256


class TextPart(StrictModel):
    type: Literal["text"] = "text"
    text: Annotated[str, Field(min_length=1, max_length=200_000)]
    language: str | None = None
    format: Literal["plain", "markdown"] = "plain"


class AudioPart(StrictModel):
    type: Literal["audio"] = "audio"
    asset_ref: AssetPointer
    codec: str | None = None
    sample_rate: Annotated[int, Field(gt=0, le=384_000)] | None = None


class ImagePart(StrictModel):
    type: Literal["image"] = "image"
    asset_ref: AssetPointer
    width: Annotated[int, Field(gt=0)] | None = None
    height: Annotated[int, Field(gt=0)] | None = None


class VideoPart(StrictModel):
    type: Literal["video"] = "video"
    asset_ref: AssetPointer
    duration_ms: Annotated[int, Field(gt=0)] | None = None


class FilePart(StrictModel):
    type: Literal["file"] = "file"
    asset_ref: AssetPointer
    display_name: Annotated[str, Field(min_length=1, max_length=255)]


class TelemetryPart(StrictModel):
    type: Literal["telemetry"] = "telemetry"
    channel: Annotated[str, Field(min_length=1, max_length=120)]
    value: JsonScalar
    unit: str | None = None
    quality: Annotated[float, Field(ge=0, le=1)] | None = None


class InteractionPart(StrictModel):
    type: Literal["interaction"] = "interaction"
    name: NamespacedName
    target: str | None = None
    value: JsonScalar = None


class ControlPart(StrictModel):
    type: Literal["control"] = "control"
    name: NamespacedName
    args: JsonArgs = Field(default_factory=dict)


class AssetRefPart(StrictModel):
    type: Literal["asset_ref"] = "asset_ref"
    asset_ref: AssetPointer


class SpeechPart(StrictModel):
    type: Literal["speech"] = "speech"
    text: Annotated[str, Field(min_length=1, max_length=20_000)]
    voice_profile: str | None = None
    language: str | None = None


class NotificationPart(StrictModel):
    type: Literal["notification"] = "notification"
    title: Annotated[str, Field(min_length=1, max_length=200)]
    body: Annotated[str, Field(min_length=1, max_length=4_000)]
    category: NamespacedName


class DeviceCommandPart(StrictModel):
    type: Literal["device_command"] = "device_command"
    tool_execution_id: UUID7
    command: NamespacedName
    args_redacted: JsonArgs = Field(default_factory=dict)


class AudioStreamPart(StrictModel):
    type: Literal["audio_stream"] = "audio_stream"
    stream_id: Annotated[str, Field(min_length=1, max_length=160)]
    codec: str
    sample_rate: Annotated[int, Field(gt=0, le=384_000)]


class VideoStreamPart(StrictModel):
    type: Literal["video_stream"] = "video_stream"
    stream_id: Annotated[str, Field(min_length=1, max_length=160)]
    codec: str
    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]


InputContentPart: TypeAlias = Annotated[
    TextPart
    | AudioPart
    | ImagePart
    | VideoPart
    | FilePart
    | InteractionPart
    | ControlPart
    | AssetRefPart,
    Field(discriminator="type"),
]
EphemeralContentPart: TypeAlias = Annotated[
    TelemetryPart | AudioStreamPart | VideoStreamPart,
    Field(discriminator="type"),
]
OutputContentPart: TypeAlias = Annotated[
    TextPart
    | SpeechPart
    | AudioPart
    | ImagePart
    | VideoPart
    | FilePart
    | NotificationPart
    | DeviceCommandPart,
    Field(discriminator="type"),
]

