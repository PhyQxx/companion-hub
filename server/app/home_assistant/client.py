from __future__ import annotations

import contextlib
import json
import ssl
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect

from .models import (
    HomeAssistantError,
    HomeAssistantLogEntry,
    HomeAssistantState,
    HomeAssistantStateChange,
)


class HomeAssistantClient:
    """Minimal authenticated HA REST/WebSocket client with payload-free errors."""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        verify_tls: bool = True,
        connect_timeout_ms: int = 5_000,
        request_timeout_ms: int = 8_000,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._verify_tls = verify_tls
        self._connect_timeout = connect_timeout_ms / 1_000
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=httpx.Timeout(request_timeout_ms / 1_000),
            verify=verify_tls,
        )

    async def fetch_states(self) -> tuple[HomeAssistantState, ...]:
        try:
            response = await self._http.get("/api/states", headers=self._headers)
        except httpx.TimeoutException as error:
            raise HomeAssistantError("ha_timeout") from error
        except httpx.HTTPError as error:
            raise HomeAssistantError("ha_offline") from error
        if response.status_code in {401, 403}:
            raise HomeAssistantError("ha_auth_failed")
        if response.status_code >= 400:
            raise HomeAssistantError("ha_request_failed")
        try:
            payload = response.json()
        except ValueError as error:
            raise HomeAssistantError("ha_response_invalid") from error
        if not isinstance(payload, list):
            raise HomeAssistantError("ha_response_invalid")
        states: list[HomeAssistantState] = []
        for item in payload:
            state = _parse_state(item)
            if state is not None:
                states.append(state)
        return tuple(states)

    async def fetch_entity_areas(self) -> dict[str, str]:
        """通过 HA Template API 获取实体 ID 到区域名称的映射。"""
        template = (
            "{% set ns = namespace(areas={}) %}"
            "{% for state in states %}"
            "{% set area = area_name(state.entity_id) %}"
            "{% if area %}"
            "{% set _ = ns.areas.update({state.entity_id: area}) %}"
            "{% endif %}"
            "{% endfor %}"
            "{{ ns.areas | tojson }}"
        )
        try:
            response = await self._http.post(
                "/api/template",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"template": template},
            )
        except httpx.TimeoutException as error:
            raise HomeAssistantError("ha_timeout") from error
        except httpx.HTTPError as error:
            raise HomeAssistantError("ha_offline") from error
        _raise_for_status(response)
        text = response.text.strip()
        # HA template endpoint returns quoted JSON string inside HTML or plain text
        # Remove surrounding quotes if present
        if text.startswith('"') and text.endswith('"'):
            with contextlib.suppress(ValueError):
                text = json.loads(text)
        try:
            payload = json.loads(text)
        except ValueError as error:
            raise HomeAssistantError("ha_response_invalid") from error
        if not isinstance(payload, dict):
            raise HomeAssistantError("ha_response_invalid")
        return {
            str(k): str(v) for k, v in payload.items() if isinstance(k, str) and isinstance(v, str)
        }

    async def fetch_entity_devices(self) -> dict[str, dict[str, str | None]]:
        """Return HA device-registry metadata keyed by entity ID."""
        template = (
            "{% set ns = namespace(devices={}) %}"
            "{% for state in states %}"
            "{% set did = device_id(state.entity_id) %}"
            "{% if did %}"
            "{% set _ = ns.devices.update({state.entity_id: {"
            "'device_id': did,"
            "'name': device_attr(did, 'name'),"
            "'manufacturer': device_attr(did, 'manufacturer'),"
            "'model': device_attr(did, 'model')"
            "}}) %}"
            "{% endif %}"
            "{% endfor %}"
            "{{ ns.devices | tojson }}"
        )
        try:
            response = await self._http.post(
                "/api/template",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"template": template},
            )
        except httpx.TimeoutException as error:
            raise HomeAssistantError("ha_timeout") from error
        except httpx.HTTPError as error:
            raise HomeAssistantError("ha_offline") from error
        _raise_for_status(response)
        text = response.text.strip()
        if text.startswith('"') and text.endswith('"'):
            with contextlib.suppress(ValueError):
                text = json.loads(text)
        try:
            payload = json.loads(text)
        except ValueError as error:
            raise HomeAssistantError("ha_response_invalid") from error
        if not isinstance(payload, dict):
            raise HomeAssistantError("ha_response_invalid")
        devices: dict[str, dict[str, str | None]] = {}
        for entity_id, raw in payload.items():
            if not isinstance(entity_id, str) or not isinstance(raw, dict):
                continue
            devices[entity_id] = {
                key: str(raw[key]) if raw.get(key) is not None else None
                for key in ("device_id", "name", "manufacturer", "model")
            }
        return devices

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        service_data: dict[str, Any] | None = None,
    ) -> tuple[HomeAssistantState, ...]:
        payload: dict[str, Any] = {"entity_id": entity_id}
        payload.update(service_data or {})
        try:
            response = await self._http.post(
                f"/api/services/{domain}/{service}",
                headers=self._headers,
                json=payload,
            )
        except httpx.TimeoutException as error:
            raise HomeAssistantError("ha_timeout") from error
        except httpx.HTTPError as error:
            raise HomeAssistantError("ha_offline") from error
        _raise_for_status(response)
        try:
            raw = response.json()
        except ValueError as error:
            raise HomeAssistantError("ha_response_invalid") from error
        if not isinstance(raw, list):
            raise HomeAssistantError("ha_response_invalid")
        return tuple(state for item in raw if (state := _parse_state(item)) is not None)

    async def fetch_history(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
    ) -> tuple[HomeAssistantState, ...]:
        path = f"/api/history/period/{quote(start.isoformat(), safe='')}"
        try:
            response = await self._http.get(
                path,
                headers=self._headers,
                params={
                    "end_time": end.isoformat(),
                    "filter_entity_id": entity_id,
                    "minimal_response": "true",
                    "no_attributes": "true",
                },
            )
        except httpx.TimeoutException as error:
            raise HomeAssistantError("ha_timeout") from error
        except httpx.HTTPError as error:
            raise HomeAssistantError("ha_offline") from error
        _raise_for_status(response)
        try:
            raw = response.json()
        except ValueError as error:
            raise HomeAssistantError("ha_response_invalid") from error
        if not isinstance(raw, list):
            raise HomeAssistantError("ha_response_invalid")
        rows = raw[0] if raw and isinstance(raw[0], list) else []
        states: list[HomeAssistantState] = []
        for item in rows:
            state = _parse_state(item, fallback_entity_id=entity_id)
            if state is not None:
                states.append(state)
        return tuple(states)

    async def fetch_logbook(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
    ) -> tuple[HomeAssistantLogEntry, ...]:
        path = f"/api/logbook/{quote(start.isoformat(), safe='')}"
        try:
            response = await self._http.get(
                path,
                headers=self._headers,
                params={"end_time": end.isoformat(), "entity": entity_id},
            )
        except httpx.TimeoutException as error:
            raise HomeAssistantError("ha_timeout") from error
        except httpx.HTTPError as error:
            raise HomeAssistantError("ha_offline") from error
        _raise_for_status(response)
        try:
            raw = response.json()
        except ValueError as error:
            raise HomeAssistantError("ha_response_invalid") from error
        if not isinstance(raw, list):
            raise HomeAssistantError("ha_response_invalid")
        entries: list[HomeAssistantLogEntry] = []
        for item in raw:
            if not isinstance(item, dict) or item.get("entity_id") != entity_id:
                continue
            entries.append(
                HomeAssistantLogEntry(
                    entity_id=entity_id,
                    when=_parse_datetime(item.get("when")),
                    name=str(item["name"]) if item.get("name") is not None else None,
                    message=(str(item["message"]) if item.get("message") is not None else None),
                    domain=(str(item["domain"]) if item.get("domain") is not None else None),
                )
            )
        return tuple(entries)

    async def state_changes(self) -> AsyncIterator[HomeAssistantStateChange]:
        ssl_context: ssl.SSLContext | None = None
        if self.websocket_url.startswith("wss://"):
            ssl_context = (
                ssl.create_default_context()
                if self._verify_tls
                else ssl._create_unverified_context()
            )
        try:
            async with connect(
                self.websocket_url,
                ssl=ssl_context,
                open_timeout=self._connect_timeout,
                max_size=2 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
            ) as websocket:
                required = await _receive_json(websocket.recv)
                if required.get("type") != "auth_required":
                    raise HomeAssistantError("ha_protocol_invalid")
                await websocket.send(
                    json.dumps({"type": "auth", "access_token": self._access_token})
                )
                authenticated = await _receive_json(websocket.recv)
                if authenticated.get("type") == "auth_invalid":
                    raise HomeAssistantError("ha_auth_failed")
                if authenticated.get("type") != "auth_ok":
                    raise HomeAssistantError("ha_protocol_invalid")
                await websocket.send(
                    json.dumps(
                        {
                            "id": 1,
                            "type": "subscribe_events",
                            "event_type": "state_changed",
                        }
                    )
                )
                subscribed = await _receive_json(websocket.recv)
                if (
                    subscribed.get("type") != "result"
                    or subscribed.get("id") != 1
                    or subscribed.get("success") is not True
                ):
                    raise HomeAssistantError("ha_subscription_failed")
                async for raw in websocket:
                    message = _decode_json(raw)
                    change = _parse_state_change(message)
                    if change is not None:
                        yield change
        except HomeAssistantError:
            raise
        except TimeoutError as error:
            raise HomeAssistantError("ha_timeout") from error
        except Exception as error:
            raise HomeAssistantError("ha_offline") from error

    @property
    def websocket_url(self) -> str:
        parsed = urlsplit(self._base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit((scheme, parsed.netloc, "/api/websocket", "", ""))

    async def close(self) -> None:
        if self._owns_http_client:
            await self._http.aclose()


async def _receive_json(receiver: Any) -> dict[str, Any]:
    return _decode_json(await receiver())


def _decode_json(raw: str | bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise HomeAssistantError("ha_protocol_invalid") from error
    if not isinstance(payload, dict):
        raise HomeAssistantError("ha_protocol_invalid")
    return payload


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code in {401, 403}:
        raise HomeAssistantError("ha_auth_failed")
    if response.status_code == 404:
        raise HomeAssistantError("ha_entity_not_found")
    if response.status_code >= 400:
        raise HomeAssistantError("ha_request_failed")


def _parse_state(
    payload: Any,
    *,
    fallback_entity_id: str | None = None,
) -> HomeAssistantState | None:
    if not isinstance(payload, dict):
        return None
    entity_id = payload.get("entity_id", fallback_entity_id)
    state = payload.get("state")
    attributes = payload.get("attributes", {})
    if not isinstance(entity_id, str) or not isinstance(state, str):
        return None
    if not isinstance(attributes, dict):
        attributes = {}
    return HomeAssistantState(
        entity_id=entity_id,
        state=state,
        attributes={str(key): value for key, value in attributes.items()},
        last_changed=_parse_datetime(payload.get("last_changed")),
        last_updated=_parse_datetime(payload.get("last_updated")),
    )


def _parse_state_change(message: dict[str, Any]) -> HomeAssistantStateChange | None:
    if message.get("type") != "event":
        return None
    event = message.get("event")
    if not isinstance(event, dict) or event.get("event_type") != "state_changed":
        return None
    data = event.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("entity_id"), str):
        return None
    entity_id = data["entity_id"]
    new_state = data.get("new_state")
    if new_state is None:
        return HomeAssistantStateChange(entity_id=entity_id, new_state=None)
    parsed = _parse_state(new_state)
    if parsed is None or parsed.entity_id != entity_id:
        return None
    return HomeAssistantStateChange(entity_id=entity_id, new_state=parsed)
