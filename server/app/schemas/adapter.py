from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from .common import NamespacedName, PrivacyLevel, SchemaRef, StrictModel, TokenName

SEMVER_PATTERN = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$"


class AdapterDirection(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class AdapterState(StrEnum):
    INSTALLED = "installed"
    CONFIGURED = "configured"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class DeliveryCapabilities(StrictModel):
    supports_ack: bool = False
    supports_cancel: bool = False
    supports_replace: bool = False
    proactive_reachable: bool = False


class PresentationCapabilities(StrictModel):
    engine: str | None = None
    emotions: list[str] = Field(default_factory=list)
    gestures: list[str] = Field(default_factory=list)


class AdapterCapabilities(StrictModel):
    input_parts: list[str] = Field(default_factory=list)
    output_parts: list[str] = Field(default_factory=list)
    streaming: list[str] = Field(default_factory=list)
    presentation: PresentationCapabilities = Field(default_factory=PresentationCapabilities)
    delivery: DeliveryCapabilities = Field(default_factory=DeliveryCapabilities)
    critical: list[TokenName] = Field(default_factory=list)


class AdapterPrivacy(StrictModel):
    runs_local: bool
    max_input_level: PrivacyLevel
    max_output_level: PrivacyLevel


class InputPolicy(StrictModel):
    backpressure: Literal["block", "drop_oldest", "sample", "aggregate"] = "block"
    queue_limit: Annotated[int, Field(gt=0, le=1_000_000)] = 100


class AdapterManifest(StrictModel):
    schema_version: Literal[1] = 1
    adapter_id: NamespacedName
    adapter_version: Annotated[str, Field(pattern=SEMVER_PATTERN)]
    min_hub_version: Annotated[str, Field(pattern=SEMVER_PATTERN)]
    max_tested_hub_version: Annotated[str, Field(min_length=1, max_length=64)]
    direction: Annotated[list[AdapterDirection], Field(min_length=1, max_length=2)]
    transport_bindings: list[str] = Field(default_factory=list, max_length=16)
    config_schema_ref: SchemaRef
    capabilities: AdapterCapabilities
    privacy: AdapterPrivacy
    permissions: list[TokenName] = Field(default_factory=list, max_length=32)
    input_policy: InputPolicy = Field(default_factory=InputPolicy)
    resource_profile: TokenName


class AdapterHealth(StrictModel):
    state: AdapterState
    checked_at: AwareDatetime
    reason_code: TokenName | None = None


class EndpointCapabilities(StrictModel):
    endpoint_id: Annotated[str, Field(min_length=1, max_length=160)]
    capabilities: AdapterCapabilities
    observed_at: AwareDatetime
