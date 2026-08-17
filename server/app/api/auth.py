import asyncio
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from app.auth import (
    AuthService,
    AuthSession,
    AuthSetupExists,
    ChatPrincipal,
    InvalidCredentials,
    InvalidSession,
)
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard

_CHAT_BEARER = HTTPBearer(auto_error=False)
ChatCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_CHAT_BEARER)]


class SetupStatusResponse(StrictModel):
    setup_required: bool


class SetupRequest(StrictModel):
    display_name: Annotated[str, Field(min_length=1, max_length=160)] = "主人"
    password: Annotated[str, Field(min_length=12, max_length=256)]


class LoginRequest(StrictModel):
    password: Annotated[str, Field(min_length=1, max_length=256)]


class UserResponse(StrictModel):
    id: UUID
    display_name: str


class SessionResponse(StrictModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserResponse


class MeResponse(StrictModel):
    session_id: UUID
    expires_at: datetime
    user: UserResponse


class ChatSessionGuard:
    def __init__(self, service: AuthService) -> None:
        self._service = service

    async def __call__(self, credentials: ChatCredentials) -> ChatPrincipal:
        if credentials is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="chat session required")
        try:
            return await self._service.authenticate(credentials.credentials)
        except InvalidSession as error:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, detail="invalid or expired chat session"
            ) from error


class LoginThrottle:
    def __init__(self, *, limit: int = 5, window_seconds: float = 300) -> None:
        self._limit = limit
        self._window = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        async with self._lock:
            failures = self._active(key)
            if len(failures) >= self._limit:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="too many login attempts",
                    headers={"Retry-After": str(int(self._window))},
                )

    async def failure(self, key: str) -> None:
        async with self._lock:
            self._active(key).append(time.monotonic())

    async def success(self, key: str) -> None:
        async with self._lock:
            self._failures.pop(key, None)

    def _active(self, key: str) -> deque[float]:
        failures = self._failures[key]
        cutoff = time.monotonic() - self._window
        while failures and failures[0] < cutoff:
            failures.popleft()
        return failures


def create_auth_router(service: AuthService, *, admin_token: str | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
    chat_guard = ChatSessionGuard(service)
    throttle = LoginThrottle()

    @router.get("/status", response_model=SetupStatusResponse)
    async def setup_status() -> SetupStatusResponse:
        return SetupStatusResponse(setup_required=await service.setup_required())

    @router.post(
        "/setup",
        response_model=SessionResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )
    async def setup(body: SetupRequest) -> SessionResponse:
        try:
            result = await service.setup(
                display_name=body.display_name, password=body.password
            )
        except AuthSetupExists as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        return _session_response(result)

    @router.post("/login", response_model=SessionResponse)
    async def login(body: LoginRequest, request: Request) -> SessionResponse:
        key = request.client.host if request.client is not None else "unknown"
        await throttle.check(key)
        try:
            result = await service.login(password=body.password)
        except InvalidCredentials as error:
            await throttle.failure(key)
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, detail="invalid chat credential"
            ) from error
        await throttle.success(key)
        return _session_response(result)

    @router.get("/me", response_model=MeResponse)
    async def me(principal: Annotated[ChatPrincipal, Depends(chat_guard)]) -> MeResponse:
        return _me_response(principal)

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(principal: Annotated[ChatPrincipal, Depends(chat_guard)]) -> None:
        await service.logout(principal)

    return router


def _session_response(value: AuthSession) -> SessionResponse:
    return SessionResponse(
        access_token=value.access_token,
        expires_at=value.principal.expires_at,
        user=UserResponse(
            id=value.principal.user_id,
            display_name=value.principal.display_name,
        ),
    )


def _me_response(value: ChatPrincipal) -> MeResponse:
    return MeResponse(
        session_id=value.session_id,
        expires_at=value.expires_at,
        user=UserResponse(id=value.user_id, display_name=value.display_name),
    )
