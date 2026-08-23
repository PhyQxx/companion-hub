from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from app.devices import (
    DeviceAliasConflict,
    DeviceCredentialInvalid,
    DeviceNotFound,
    DevicePrincipal,
    DeviceRegistry,
    DeviceRegistryError,
    DeviceRevisionConflict,
    DeviceSnapshot,
    PairingCodeInvalid,
)
from app.schemas.common import NamespacedName, StrictModel

from .admin_config import AdminTokenGuard

_DEVICE_BEARER = HTTPBearer(auto_error=False)
DeviceCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_DEVICE_BEARER)]
CapabilityList = Annotated[list[NamespacedName], Field(max_length=64)]


class PairingCodeRequest(StrictModel):
    owner_user_id: UUID | None = None
    granted_capabilities: CapabilityList = Field(default_factory=list)
    ttl_seconds: Annotated[int, Field(ge=60, le=3600)] = 600


class PairingCodeResponse(StrictModel):
    pairing_code: str
    owner_user_id: UUID
    granted_capabilities: CapabilityList
    expires_at: datetime


class PairDeviceRequest(StrictModel):
    pairing_code: Annotated[str, Field(min_length=20, max_length=256)]
    name: Annotated[str, Field(min_length=1, max_length=160)]
    alias: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    client_type: Literal["desktop", "browser", "mobile", "iot", "other"]
    capabilities: CapabilityList = Field(default_factory=list)


class HeartbeatRequest(StrictModel):
    capabilities: CapabilityList = Field(default_factory=list)


class UpdateDeviceRequest(StrictModel):
    expected_revision: Annotated[int, Field(ge=1)]
    name: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    alias: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    granted_capabilities: CapabilityList | None = None


class DeviceResponse(StrictModel):
    id: UUID
    owner_user_id: UUID
    name: str
    alias: str | None
    client_type: str
    capabilities: list[str]
    granted_capabilities: list[str]
    effective_capabilities: list[str]
    revision: int
    paired_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None
    online: bool


class PairDeviceResponse(StrictModel):
    access_token: str
    token_type: str = "bearer"
    device: DeviceResponse


class DeviceCredentialGuard:
    def __init__(self, registry: DeviceRegistry) -> None:
        self._registry = registry

    async def __call__(self, credentials: DeviceCredentials) -> DevicePrincipal:
        if credentials is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="device credential required")
        try:
            return await self._registry.authenticate(credentials.credentials)
        except DeviceCredentialInvalid as error:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, detail="invalid or revoked device credential"
            ) from error


def create_device_routers(
    registry: DeviceRegistry,
    *,
    admin_token: str | None,
) -> tuple[APIRouter, APIRouter]:
    admin = APIRouter(
        prefix="/api/v1/admin/devices",
        tags=["admin-devices"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )
    devices = APIRouter(prefix="/api/v1/devices", tags=["devices"])
    device_guard = DeviceCredentialGuard(registry)
    device_auth = Depends(device_guard)

    @admin.post(
        "/pairing-codes",
        response_model=PairingCodeResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_pairing_code(body: PairingCodeRequest) -> PairingCodeResponse:
        try:
            result = await registry.create_pairing_code(
                owner_user_id=body.owner_user_id,
                granted_capabilities=tuple(body.granted_capabilities),
                ttl_seconds=body.ttl_seconds,
            )
        except DeviceRegistryError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        return PairingCodeResponse(
            pairing_code=result.code,
            owner_user_id=result.owner_user_id,
            granted_capabilities=list(result.granted_capabilities),
            expires_at=result.expires_at,
        )

    @admin.get("", response_model=list[DeviceResponse])
    async def list_devices(owner_user_id: UUID | None = None) -> list[DeviceResponse]:
        return [
            _device_response(value)
            for value in await registry.list_devices(owner_user_id=owner_user_id)
        ]

    @admin.patch("/{device_id}", response_model=DeviceResponse)
    async def update_device(device_id: UUID, body: UpdateDeviceRequest) -> DeviceResponse:
        try:
            result = await registry.update_device(
                device_id,
                expected_revision=body.expected_revision,
                name=body.name,
                alias=body.alias,
                alias_supplied="alias" in body.model_fields_set,
                granted_capabilities=(
                    tuple(body.granted_capabilities)
                    if body.granted_capabilities is not None
                    else None
                ),
            )
        except DeviceNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except DeviceRevisionConflict as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        except DeviceAliasConflict as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        except DeviceRegistryError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        return _device_response(result)

    @admin.post("/{device_id}/revoke", response_model=DeviceResponse)
    async def revoke_device(device_id: UUID) -> DeviceResponse:
        try:
            return _device_response(await registry.revoke(device_id))
        except DeviceNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @devices.post(
        "/pair",
        response_model=PairDeviceResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def pair_device(body: PairDeviceRequest) -> PairDeviceResponse:
        try:
            result = await registry.pair(
                pairing_code=body.pairing_code,
                name=body.name,
                alias=body.alias,
                client_type=body.client_type,
                capabilities=tuple(body.capabilities),
            )
        except PairingCodeInvalid as error:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error
        except DeviceAliasConflict as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        except DeviceRegistryError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        return PairDeviceResponse(
            access_token=result.access_token,
            device=_device_response(result.device),
        )

    @devices.post("/heartbeat", response_model=DeviceResponse)
    async def heartbeat(
        body: HeartbeatRequest,
        principal: DevicePrincipal = device_auth,
    ) -> DeviceResponse:
        try:
            return _device_response(
                await registry.heartbeat(
                    principal,
                    capabilities=tuple(body.capabilities),
                )
            )
        except DeviceCredentialInvalid as error:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, detail="invalid or revoked device credential"
            ) from error

    return admin, devices


def _device_response(value: DeviceSnapshot) -> DeviceResponse:
    return DeviceResponse(
        id=value.id,
        owner_user_id=value.owner_user_id,
        name=value.name,
        alias=value.alias,
        client_type=value.client_type,
        capabilities=list(value.capabilities),
        granted_capabilities=list(value.granted_capabilities),
        effective_capabilities=list(value.effective_capabilities),
        revision=value.revision,
        paired_at=value.paired_at,
        last_seen_at=value.last_seen_at,
        revoked_at=value.revoked_at,
        online=value.online,
    )
