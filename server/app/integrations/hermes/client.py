"""hermes api_server HTTP 边界：httpx 只允许出现在本文件。

api_server 契约（2026-10-12 对 0.20.2 实测）：
- Bearer API_SERVER_KEY；无 key 401。
- POST /v1/runs {"input": "<自然语言指令>"} → 202 {"run_id": ...}。
- GET /v1/runs/{run_id} → 状态字典；status ∈ queued/running/
  waiting_for_approval/stopping/completed/cancelled/failed，终态附带
  usage 与结果文本（字段名做防御式提取，见 _extract_output）。
- POST /v1/runs/{run_id}/stop → 请求中断。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import httpx

logger = logging.getLogger("app.integrations.hermes")

TERMINAL_STATUSES = frozenset({"completed", "cancelled", "failed", "stopped"})
PENDING_APPROVAL_STATUSES = frozenset({"waiting_for_approval", "awaiting_approval"})
RUNNING_STATUSES = frozenset({"queued", "pending", "running", "started", "starting", "stopping"})


class HermesClientError(RuntimeError):
    def __init__(self, reason_code: str, *, detail: str | None = None) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(reason_code)


class HermesHttpClient:
    """有状态极薄的 HTTP 客户端；每次调用独立超时与重试策略由调用方管理。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        verify_tls: bool = True,
        connect_timeout_seconds: float = 5.0,
        request_timeout_seconds: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._verify_tls = verify_tls
        self._connect_timeout_seconds = connect_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            verify=self._verify_tls,
            timeout=httpx.Timeout(
                self._request_timeout_seconds,
                connect=self._connect_timeout_seconds,
            ),
            follow_redirects=False,
        )

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            async with self._client() as client:
                response = await client.request(method, path, json=json)
        except httpx.TimeoutException as error:
            raise HermesClientError("hermes_timeout", detail=path) from error
        except httpx.HTTPError as error:
            raise HermesClientError("hermes_unreachable", detail=path) from error
        if response.status_code in {401, 403}:
            raise HermesClientError("hermes_auth_failed", detail=str(response.status_code))
        if response.status_code >= 500:
            raise HermesClientError("hermes_server_error", detail=str(response.status_code))
        try:
            payload = response.json()
        except ValueError as error:
            raise HermesClientError("hermes_payload_invalid", detail=path) from error
        if response.status_code >= 400:
            message = payload.get("error", {}).get("message") if isinstance(payload, dict) else None
            raise HermesClientError(
                "hermes_request_rejected",
                detail=f"{response.status_code}:{str(message)[:200]}",
            )
        return payload if isinstance(payload, dict) else {}

    async def health(self) -> dict[str, Any]:
        try:
            async with self._client() as client:
                response = await client.get("/health")
        except httpx.TimeoutException as error:
            raise HermesClientError("hermes_timeout", detail="/health") from error
        except httpx.HTTPError as error:
            raise HermesClientError("hermes_unreachable", detail="/health") from error
        if response.status_code != 200:
            raise HermesClientError(
                "hermes_unhealthy", detail=str(response.status_code)
            )
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as error:
            raise HermesClientError("hermes_payload_invalid", detail="/health") from error
        return payload

    async def capabilities(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/capabilities")

    async def submit_run(self, instruction: str) -> str:
        payload = await self._request("POST", "/v1/runs", json={"input": instruction})
        run_id = payload.get("run_id") or payload.get("id")
        if not isinstance(run_id, str) or not run_id:
            # 请求已被受理但响应损坏：结果未知，调用方不得自动重试。
            raise HermesClientError("hermes_submit_unknown", detail="run_id_missing")
        return run_id

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/runs/{run_id}")

    async def get_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        payload = await self._request("GET", f"/api/sessions/{session_id}/messages")
        data = payload.get("data")
        return data if isinstance(data, list) else []

    async def stop_run(self, run_id: str) -> None:
        await self._request("POST", f"/v1/runs/{run_id}/stop", json={})


def hermes_client_factory(
    base_url: str,
    api_key: str,
    *,
    verify_tls: bool = True,
    connect_timeout_seconds: float = 5.0,
    request_timeout_seconds: float = 15.0,
) -> HermesHttpClient:
    return HermesHttpClient(
        base_url,
        api_key,
        verify_tls=verify_tls,
        connect_timeout_seconds=connect_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
    )


def extract_run_status(payload: dict[str, Any]) -> str:
    """归一化 run 状态；未知值按仍运行处理，绝不提前判终态。"""
    raw = str(payload.get("status") or "").strip().lower()
    if raw in TERMINAL_STATUSES:
        return raw
    if raw in PENDING_APPROVAL_STATUSES:
        return "waiting_for_approval"
    if raw in RUNNING_STATUSES:
        return raw
    return "running"


def extract_run_error(payload: dict[str, Any]) -> str:
    value = payload.get("error")
    return value.strip() if isinstance(value, str) else ""


def extract_assistant_text(messages: list[dict[str, Any]]) -> str:
    """从会话消息取最新一条 assistant 文本；order=latest（新在前）。"""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "assistant":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def extract_run_output(payload: dict[str, Any]) -> str:
    """防御式提取结果文本；0.20.2 实测 completed 状态不含正文，正文经
    会话消息端点获取（见 HermesManager.watch_task），此处仅兜底兼容。"""
    for key in ("final_response", "output", "result", "response"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            content = last.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def new_request_id() -> str:
    return uuid4().hex
